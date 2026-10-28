"""User management (M3.5): admins manage users; everyone can change their own password.

``/users`` routes are admin only, ``POST /me/password`` is for any role. Every change is
audited. Passwords are write-only: no response, log line or audit entry carries one or
its hash (audit entries record ``"password": "<changed>"``).

The last admin can't be demoted or deleted (409). An admin can't delete their own
account either (409): another admin has to.
"""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.auth import MAX_PASSWORD_LENGTH, MIN_PASSWORD_LENGTH, USERNAME_PATTERN
from app.api.deps import AdminDep, AuthDep, DbDep, get_login_failures, get_passwords
from app.core import audit, users
from app.core.auth import Passwords
from app.core.login_failures import UserLoginFailures
from app.models import Role, User

router = APIRouter()

PasswordsDep = Annotated[Passwords, Depends(get_passwords)]
CounterDep = Annotated[UserLoginFailures, Depends(get_login_failures)]

NewPassword = Annotated[
    SecretStr, Field(min_length=MIN_PASSWORD_LENGTH, max_length=MAX_PASSWORD_LENGTH)
]


class UserOut(BaseModel):
    """A user as the API shows it. There is no password field, not even a hash."""

    id: int
    username: str
    role: Role
    created_at: datetime
    last_login_at: datetime | None


class UserCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(pattern=USERNAME_PATTERN)
    password: NewPassword
    role: Role


class UserUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Role


class PasswordReset(BaseModel):
    model_config = ConfigDict(extra="forbid")
    password: NewPassword


class PasswordChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    current_password: SecretStr = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)
    new_password: NewPassword


def _out(user: User) -> UserOut:
    return UserOut(
        id=user.id,
        username=user.username,
        role=user.role,
        created_at=user.created_at,
        last_login_at=user.last_login_at,
    )


def _not_found() -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, "user not found")


def _get(db: Session, user_id: int) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise _not_found()
    return user


def _last_admin() -> HTTPException:
    return HTTPException(
        status.HTTP_409_CONFLICT,
        "this is the last admin: make another user an admin first",
    )


@router.get("/users")
def list_users(_auth: AdminDep, db: DbDep) -> list[UserOut]:
    return [_out(u) for u in db.scalars(select(User).order_by(User.id))]


@router.post("/users", status_code=status.HTTP_201_CREATED)
def create_user(body: UserCreate, auth: AdminDep, db: DbDep, passwords: PasswordsDep) -> UserOut:
    taken = HTTPException(status.HTTP_409_CONFLICT, "a user with that username already exists")
    # Checked before the slow hash, and again (case-insensitively) when inserting.
    if users.username_taken(db, body.username):
        raise taken
    db.commit()  # hold no write lock (SQLite) through the slow hash
    password_hash = passwords.hash(body.password.get_secret_value())
    try:
        user = users.add_user(
            db, username=body.username, password_hash=password_hash, role=body.role
        )
        audit.record(
            db,
            actor_type=audit.ActorType.USER,
            action=audit.Action.CREATE,
            target_type="user",
            target_id=user.id,
            user_id=auth.user.id,
            changes={"username": user.username, "role": user.role, "password": audit.REDACTED},
        )
        users.commit_new_user(db, body.username)
    except users.UsernameTaken:
        raise taken from None
    return _out(user)


@router.patch("/users/{user_id}")
def update_user(user_id: int, body: UserUpdate, auth: AdminDep, db: DbDep) -> UserOut:
    """Change a user's role. Takes effect on their next request; sessions stay."""
    users.lock_admins(db)
    user = _get(db, user_id)
    old = user.role
    if body.role == old:
        return _out(user)
    user.role = body.role
    if users.admins_left(db) == 0:
        db.rollback()
        raise _last_admin()
    audit.record(
        db,
        actor_type=audit.ActorType.USER,
        action=audit.Action.UPDATE,
        target_type="user",
        target_id=user.id,
        user_id=auth.user.id,
        changes=audit.diff({"role": old}, {"role": body.role}),
    )
    db.commit()
    return _out(user)


@router.post("/users/{user_id}/password", status_code=status.HTTP_204_NO_CONTENT)
def reset_password(
    user_id: int, body: PasswordReset, auth: AdminDep, db: DbDep, passwords: PasswordsDep
) -> Response:
    """Set another user's password and end all of their sessions.

    Resetting your own password here ends your own session too; ``POST /me/password``
    keeps it.
    """
    _get(db, user_id)
    db.commit()  # hold no write lock (SQLite) through the slow hash
    password_hash = passwords.hash(body.password.get_secret_value())
    # Row first, then sessions: a racing login either fails or loses its session.
    result = users.set_password(db, user_id, password_hash)
    if result is None:  # deleted during the hash
        db.rollback()
        raise _not_found()
    audit.record(
        db,
        actor_type=audit.ActorType.USER,
        action=audit.Action.UPDATE,
        target_type="user",
        target_id=user_id,
        user_id=auth.user.id,
        changes={
            "password": audit.REDACTED,
            "sessions_ended": result.sessions_ended,
            "failed_logins_cleared": result.failed_logins_cleared,
        },
    )
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_user(user_id: int, auth: AdminDep, db: DbDep) -> Response:
    """Delete a user. Their sessions and failed logins go with them (cascade); audit
    entries stay, with an empty ``user_id``."""
    if user_id == auth.user.id:
        # Approved rule (AGENTS.md Contract 3): another admin has to do it.
        raise HTTPException(
            status.HTTP_409_CONFLICT, "you can't delete your own account: ask another admin"
        )
    users.lock_admins(db)
    user = _get(db, user_id)
    changes = {"username": user.username, "role": user.role}
    db.delete(user)
    if users.admins_left(db) == 0:
        db.rollback()
        raise _last_admin()
    audit.record(
        db,
        actor_type=audit.ActorType.USER,
        action=audit.Action.DELETE,
        target_type="user",
        target_id=user_id,
        user_id=auth.user.id,
        changes=changes,
    )
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/me/password", status_code=status.HTTP_204_NO_CONTENT)
def change_my_password(
    body: PasswordChange,
    auth: AuthDep,
    db: DbDep,
    passwords: PasswordsDep,
    counter: CounterDep,
) -> Response:
    """Change your own password. Needs the current one; ends your other sessions.

    A wrong current password counts as a failed login on your account, so a stolen
    session can't be used to guess it faster than the login form allows (429 once the
    account is locked).
    """
    user_id = auth.user.id
    verified_hash = auth.user.password_hash
    keep = auth.session.id
    # Counted up front, like a login. Commits, so no write lock is held (SQLite) through
    # the slow checks below.
    retry_after = counter.acquire(db, user_id)
    if retry_after is not None:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "too many failed logins, try again later",
            headers={"Retry-After": str(retry_after)},
        )
    if not passwords.verify(verified_hash, body.current_password.get_secret_value()):
        # The attempt stays counted. 400, not 401: the session itself is fine.
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "the current password is wrong")
    password_hash = passwords.hash(body.new_password.get_secret_value())
    # Only while the hash is still the verified one: a reset that landed meanwhile wins.
    result = users.set_password(
        db, user_id, password_hash, expected_hash=verified_hash, keep_session_id=keep
    )
    if result is None:
        db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, "your password was changed meanwhile: log in again"
        )
    audit.record(
        db,
        actor_type=audit.ActorType.USER,
        action=audit.Action.UPDATE,
        target_type="user",
        target_id=user_id,
        user_id=user_id,
        changes={"password": audit.REDACTED, "sessions_ended": result.sessions_ended},
    )
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
