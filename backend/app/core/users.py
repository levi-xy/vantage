"""User changes shared by the admin CLI and the user API (M3.5).

Both create users, set passwords and end sessions the same way, so the rules live here:

* **Duplicate usernames** are refused case-insensitively ("Admin" and "admin" look
  alike, and the login limiter counts a case variant on the existing account). Only a
  real duplicate is reported as :class:`UsernameTaken`; any other integrity error is
  raised as is.
* **Setting a password** writes (and so locks) the user row *before* deleting sessions.
  A login only creates a session if it can update the row while the hash is still the
  one it verified (``app.api.auth.login``). So a login racing a reset either fails, or
  holds the row first, commits its session, and that session is deleted here.
* **The last admin** can't be demoted or deleted: :func:`lock_admins` before the change,
  :func:`admins_left` after it, in the same transaction.

Nothing here commits; callers record the audit entry and commit.
"""

from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy import CursorResult, delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.login_failures import UserLoginFailures
from app.models import Role, Setting, User, UserSession

# Written in the same transaction as the first user (setup, or the CLI's create-user).
# Its primary key makes a second, concurrent setup request fail, and it keeps network
# setup closed even if every user is later deleted.
SETUP_MARKER = "setup.completed"


class UsernameTaken(Exception):
    """Another user already has this username, ignoring case."""


def username_taken(db: Session, username: str) -> bool:
    """True if a user's name equals ``username`` ignoring case."""
    found = db.scalar(select(User.id).where(func.lower(User.username) == username.lower()).limit(1))
    return found is not None


def add_user(db: Session, *, username: str, password_hash: str, role: Role) -> User:
    """Add a user and flush it. Also closes network setup, if it isn't already.

    Raises :class:`UsernameTaken` (after rolling ``db`` back) only when the name is
    really taken: checked first, and again if the insert hits an integrity error, so a
    concurrent create of the same name is reported correctly and anything else (a
    broken constraint that isn't the username) propagates as the original error.
    """
    if username_taken(db, username):
        raise UsernameTaken
    # Looked up before the user is added: the query would flush the new row outside the
    # `try` below.
    close_setup = db.get(Setting, SETUP_MARKER) is None
    user = User(username=username, password_hash=password_hash, role=role)
    db.add(user)
    if close_setup:
        db.add(Setting(key=SETUP_MARKER, value=True))
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        if username_taken(db, username):
            raise UsernameTaken from None
        raise
    return user


def commit_new_user(db: Session, username: str) -> None:
    """Commit a transaction that added ``username``, mapping a duplicate to UsernameTaken.

    Some databases only report the unique violation at commit.
    """
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        if username_taken(db, username):
            raise UsernameTaken from None
        raise


@dataclass(frozen=True)
class PasswordSet:
    sessions_ended: int
    failed_logins_cleared: int


def set_password(
    db: Session,
    user_id: int,
    password_hash: str,
    *,
    expected_hash: str | None = None,
    keep_session_id: str | None = None,
) -> PasswordSet | None:
    """Store a new hash, then end the user's sessions and clear their failed logins.

    The row is written first, then sessions are deleted (see the module docstring).
    With ``expected_hash``, the row is only written while it still holds that hash (a
    user changing their own password, after checking the current one). With
    ``keep_session_id``, that one session survives (the user's own, current one).

    Returns ``None``, having changed nothing, when the user is gone or the hash was no
    longer ``expected_hash``.
    """
    stmt = update(User).where(User.id == user_id)
    if expected_hash is not None:
        stmt = stmt.where(User.password_hash == expected_hash)
    written = cast(
        "CursorResult[Any]",
        db.execute(
            stmt.values(password_hash=password_hash).execution_options(synchronize_session=False)
        ),
    )
    if written.rowcount != 1:
        return None
    ended_stmt = delete(UserSession).where(UserSession.user_id == user_id)
    if keep_session_id is not None:
        ended_stmt = ended_stmt.where(UserSession.id != keep_session_id)
    ended = cast(
        "CursorResult[Any]", db.execute(ended_stmt.execution_options(synchronize_session=False))
    ).rowcount
    # The failures counted so far were guesses at the old password: they say nothing
    # about the new one, and keeping them would leave a user locked out after the reset
    # that was meant to let them back in.
    cleared = UserLoginFailures.clear(db, user_id)
    return PasswordSet(sessions_ended=ended, failed_logins_cleared=cleared)


def lock_admins(db: Session) -> None:
    """Lock every admin row until the transaction ends (Postgres; a no-op on SQLite).

    Call before demoting or deleting a user, then check :func:`admins_left` after the
    change. Two admins demoting each other at once would otherwise both see the other
    one still in place and leave no admin. On SQLite writers are serialised, and the
    check after the change runs once this transaction holds the write lock, so it sees
    the other change.
    """
    db.execute(select(User.id).where(User.role == Role.ADMIN).with_for_update()).all()


def admins_left(db: Session) -> int:
    """How many admins there are, counting this transaction's own pending changes."""
    db.flush()
    return db.scalar(select(func.count()).select_from(User).where(User.role == Role.ADMIN)) or 0
