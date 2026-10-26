"""First-run setup, login, logout and the current user (M1.3)."""

from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import (
    AuthDep,
    DbDep,
    SettingsDep,
    clear_session_cookie,
    get_login_limiter,
    get_passwords,
    require_json,
    session_token,
    set_session_cookie,
)
from app.core.auth import LoginRateLimiter, Passwords, csrf_token, new_session_token, session_id
from app.core.config import Settings
from app.core.db import utcnow
from app.models import Role, Setting, User, UserSession

router = APIRouter()

# Written in the same transaction as the first admin. Its primary key makes a second,
# concurrent setup request fail, and it keeps setup closed even if every user is later
# deleted (an admin has to recreate users; setup can't be reopened from the network).
SETUP_MARKER = "setup.completed"

USERNAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
MIN_PASSWORD_LENGTH = 12
# Argon2 cost doesn't depend on password length, but there's no reason to accept megabytes.
MAX_PASSWORD_LENGTH = 1024

# Failed logins allowed per 15 minutes, per username and per client address.
LOGIN_LIMITS = {"user": 5, "client": 50}

PasswordsDep = Annotated[Passwords, Depends(get_passwords)]
LimiterDep = Annotated[LoginRateLimiter, Depends(get_login_limiter)]


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
    token = _start_session(db, user, settings)
    db.commit()
    set_session_cookie(response, token, settings)
    return _me(user, token)


@router.post("/auth/login", dependencies=[Depends(require_json)])
def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    db: DbDep,
    settings: SettingsDep,
    passwords: PasswordsDep,
    limiter: LimiterDep,
) -> Me:
    # TODO(maintainer): behind the bundled nginx every request comes from the frontend
    # container, so the per-client limit is effectively global. Trusting X-Forwarded-For
    # needs a "trusted proxy" setting; left out until that's decided.
    client = request.client.host if request.client else "unknown"
    user_key, client_key = f"user:{body.username.lower()}", f"client:{client}"
    # Counted as a failure up front, so parallel requests can't all slip past the limit.
    retry_after = limiter.acquire([user_key, client_key])
    if retry_after is not None:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "too many failed logins, try again later",
            headers={"Retry-After": str(retry_after)},
        )

    user = db.scalar(select(User).where(User.username == body.username))
    password = body.password.get_secret_value()
    if not passwords.verify(user.password_hash if user else None, password) or user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid username or password")
    limiter.succeeded(user_key, [client_key])

    now = utcnow()
    if passwords.needs_rehash(user.password_hash):
        user.password_hash = passwords.hash(password)
    user.last_login_at = now
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
    db.commit()
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    clear_session_cookie(response, settings)
    return response


@router.get("/me")
def me(auth: AuthDep) -> Me:
    return _me(auth.user, auth.token)
