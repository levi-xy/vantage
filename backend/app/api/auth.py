"""First-run setup, login, logout and the current user (M1.3)."""

from datetime import timedelta
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import CursorResult, delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import (
    AuthDep,
    ClientAddressDep,
    DbDep,
    SettingsDep,
    clear_session_cookie,
    get_login_failures,
    get_login_limiter,
    get_passwords,
    require_json,
    session_token,
    set_session_cookie,
)
from app.core import audit
from app.core.auth import LoginRateLimiter, Passwords, csrf_token, new_session_token, session_id
from app.core.config import Settings
from app.core.db import utcnow
from app.core.login_failures import UserLoginFailures
from app.core.users import SETUP_MARKER
from app.models import AuditLog, Role, Setting, User, UserSession

router = APIRouter()


USERNAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
MIN_PASSWORD_LENGTH = 12
# Argon2 cost doesn't depend on password length, but there's no reason to accept megabytes.
MAX_PASSWORD_LENGTH = 1024

# Failed logins allowed per 15 minutes, per username and per client address. The "user"
# limit applies to accounts in the database (app.core.login_failures) and to usernames
# that match no account in the in-memory limiter.
LOGIN_LIMITS = {"user": 5, "client": 50}
LOGIN_WINDOW_SECONDS = 900
# At most one "locked" audit entry per account in this long (M1.9), so a spray against a
# locked account can't fill the audit log.
LOCKED_AUDIT_INTERVAL = timedelta(minutes=1)

PasswordsDep = Annotated[Passwords, Depends(get_passwords)]
LimiterDep = Annotated[LoginRateLimiter, Depends(get_login_limiter)]
CounterDep = Annotated[UserLoginFailures, Depends(get_login_failures)]


class SetupStatus(BaseModel):
    setup_required: bool


class SetupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(pattern=USERNAME_PATTERN)
    password: SecretStr = Field(min_length=MIN_PASSWORD_LENGTH, max_length=MAX_PASSWORD_LENGTH)


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=64)
    password: SecretStr = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)


class Me(BaseModel):
    id: int
    username: str
    role: Role
    # Send back in the X-CSRF-Token header on every state-changing request.
    csrf_token: str


def _me(user: User, token: str) -> Me:
    return Me(id=user.id, username=user.username, role=user.role, csrf_token=csrf_token(token))


def _setup_required(db: Session) -> bool:
    users = db.scalar(select(func.count()).select_from(User))
    return users == 0 and db.get(Setting, SETUP_MARKER) is None


def _start_session(db: Session, user: User, settings: Settings) -> str:
    now = utcnow()
    token = new_session_token()
    db.add(
        UserSession(
            id=session_id(token),
            user_id=user.id,
            created_at=now,
            last_seen_at=now,
            expires_at=now + timedelta(hours=settings.session_max_hours),
        )
    )
    return token


def _setup_closed() -> HTTPException:
    return HTTPException(status.HTTP_409_CONFLICT, "setup has already been completed")


@router.get("/setup/status")
def setup_status(db: DbDep) -> SetupStatus:
    return SetupStatus(setup_required=_setup_required(db))


@router.post("/setup", status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_json)])
def setup(
    body: SetupRequest,
    response: Response,
    db: DbDep,
    settings: SettingsDep,
    passwords: PasswordsDep,
) -> Me:
    """Create the first admin and log them in. Only works while no users exist."""
    if not _setup_required(db):
        raise _setup_closed()
    now = utcnow()
    user = User(
        username=body.username,
        password_hash=passwords.hash(body.password.get_secret_value()),
        role=Role.ADMIN,
        created_at=now,
        last_login_at=now,
    )
    db.add(Setting(key=SETUP_MARKER, value=True))
    db.add(user)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise _setup_closed() from None
    audit.record(
        db,
        actor_type=audit.ActorType.USER,
        action=audit.Action.CREATE,
        target_type="user",
        target_id=user.id,
        user_id=user.id,
        changes={"username": user.username, "role": user.role, "password": body.password},
    )
    token = _start_session(db, user, settings)
    db.commit()
    set_session_cookie(response, token, settings)
    return _me(user, token)


def _record_failed_login(db: Session, user: User | None) -> None:
    """Audit a login that reached the password check and failed (the 401 path).

    Committed right away: the 401 that follows rolls back the request's transaction.
    Only a known user's stored username is kept; anything else becomes "<unknown>".
    The submitted password is never passed in. Attempts refused with 429 never reach
    the password check, so they never come through here: a flood of them can't fill
    the table. Of those, only refusals because the account is locked are audited, by
    :func:`_record_locked_login`, at most once a minute per account.
    """
    audit.record(
        db,
        actor_type=audit.ActorType.USER,
        action=audit.Action.LOGIN_FAILED,
        target_type="user",
        target_id=user.id if user else None,
        user_id=None,
        changes={"username": user.username if user else audit.UNKNOWN_USERNAME},
    )
    db.commit()


def _record_locked_login(db: Session, user_id: int) -> None:
    """Audit a login refused (429) because the account ``user_id`` is locked (M1.9).

    Records the stored username, never the submitted one (a case variant such as
    "ADMIN" is recorded as the account it counted on), and never the password. At most
    one entry per account per :data:`LOCKED_AUDIT_INTERVAL`: skipped while a ``locked``
    entry for this user is newer than that (plain ``login_failed`` entries don't count).
    Best effort: parallel refusals may each write one, which is acceptable. Committed
    right away, since the 429 that follows rolls back the request's transaction.
    """
    user = db.get(User, user_id)
    if user is None:  # deleted since it was looked up: nothing to attribute it to
        return
    recent = db.scalar(
        select(AuditLog.id)
        .where(
            AuditLog.action == audit.Action.LOGIN_FAILED.value,
            AuditLog.target_type == "user",
            AuditLog.target_id == str(user.id),
            AuditLog.at > utcnow() - LOCKED_AUDIT_INTERVAL,
            AuditLog.changes["locked"].as_boolean().is_(True),
        )
        .order_by(AuditLog.at.desc(), AuditLog.id.desc())
        .limit(1)
    )
    if recent is not None:
        return
    audit.record(
        db,
        actor_type=audit.ActorType.USER,
        action=audit.Action.LOGIN_FAILED,
        target_type="user",
        target_id=user.id,
        user_id=None,
        changes={"username": user.username, "locked": True},
    )
    db.commit()


def _case_variant_id(db: Session, username: str) -> int | None:
    """The account a username matches only case-insensitively, if any (lowest id)."""
    return db.scalar(
        select(User.id)
        .where(func.lower(User.username) == username.lower())
        .order_by(User.id)
        .limit(1)
    )


def _memory_user_key(body: LoginRequest) -> str:
    return f"user:{body.username.lower()}"


@router.post("/auth/login", dependencies=[Depends(require_json)])
def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    db: DbDep,
    settings: SettingsDep,
    passwords: PasswordsDep,
    limiter: LimiterDep,
    counter: CounterDep,
    client: ClientAddressDep,
) -> Me:
    # `client` is the real client behind a trusted proxy (VANTAGE_TRUSTED_PROXIES), else
    # the TCP peer; an IPv6 client is its /64.
    client_key = f"client:{client}"
    user = db.scalar(select(User).where(User.username == body.username))
    # Whose failure counter this attempt counts on. Login compares usernames exactly, but
    # a name that differs only in case counts on that account too, as the old lowercased
    # in-memory key did: otherwise "ADMIN" would answer 401 while "admin" is locked, and
    # how case variants behave would tell existing usernames from unknown ones.
    counted_id = user.id if user is not None else _case_variant_id(db, body.username)
    # In memory: the client, and a username that matches no account. An account's
    # counter is in the database (app.core.login_failures), so junk usernames can't
    # evict it and it survives a restart.
    memory_keys = [client_key] if counted_id is not None else [_memory_user_key(body), client_key]
    # Each counter counts the attempt as a failure up front, so parallel requests can't
    # all slip past the limit. The in-memory one goes first: a flood from one client is
    # refused without writing to the database.
    retry_after = limiter.acquire(memory_keys)
    if retry_after is None and counted_id is not None:
        retry_after = counter.acquire(db, counted_id)
        if retry_after is not None:
            limiter.refund(memory_keys)  # a refused attempt counts nothing anywhere
            # Only an account lockout is audited. Refusals by the in-memory limiter
            # (per client, or usernames that match no account) stay unaudited.
            _record_locked_login(db, counted_id)
    if retry_after is not None:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "too many failed logins, try again later",
            headers={"Retry-After": str(retry_after)},
        )

    password = body.password.get_secret_value()
    if not passwords.verify(user.password_hash if user else None, password) or user is None:
        _record_failed_login(db, user)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid username or password")

    now = utcnow()
    verified_hash = user.password_hash
    changes: dict[str, object] = {}
    new_hash = verified_hash
    if passwords.needs_rehash(verified_hash):
        new_hash = passwords.hash(password)
        changes["password_hash"] = new_hash  # stored as "<changed>"
    # Only if the hash is still the one just verified: a password reset (CLI or API)
    # committed during the slow check must win, so no session is created with the old
    # password and a rehash never overwrites the new hash. The row lock this takes also
    # orders the login against a concurrent reset, which then ends the new session
    # (app.core.users.set_password writes the row before deleting sessions).
    claimed = cast(
        "CursorResult[Any]",
        db.execute(
            update(User)
            .where(User.id == user.id, User.password_hash == verified_hash)
            .values(password_hash=new_hash, last_login_at=now)
            .execution_options(synchronize_session=False)
        ),
    )
    if claimed.rowcount != 1:
        # Read before the rollback, which expires `user`: reloading it would fail (500)
        # if the user was deleted during the password check.
        user_id = user.id
        db.rollback()
        _record_failed_login(db, db.get(User, user_id))
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid username or password")
    db.refresh(user)
    # Committed with the session below: the failures before this login no longer count.
    counter.clear(db, user.id)
    limiter.refund(memory_keys)
    audit.record(
        db,
        actor_type=audit.ActorType.USER,
        action=audit.Action.LOGIN,
        target_type="user",
        target_id=user.id,
        user_id=user.id,
        changes=changes,
    )
    # A new login replaces this browser's old session and clears out expired ones.
    old_token = session_token(request, settings)
    if old_token is not None:
        db.execute(delete(UserSession).where(UserSession.id == session_id(old_token)))
    db.execute(delete(UserSession).where(UserSession.expires_at <= now))
    token = _start_session(db, user, settings)
    db.commit()
    set_session_cookie(response, token, settings)
    return _me(user, token)


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(auth: AuthDep, db: DbDep, settings: SettingsDep) -> Response:
    db.delete(auth.session)
    audit.record(
        db,
        actor_type=audit.ActorType.USER,
        action=audit.Action.LOGOUT,
        target_type="user",
        target_id=auth.user.id,
        user_id=auth.user.id,
    )
    db.commit()
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    clear_session_cookie(response, settings)
    return response


@router.get("/me")
def me(auth: AuthDep) -> Me:
    return _me(auth.user, auth.token)
