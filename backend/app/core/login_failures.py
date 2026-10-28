"""Failed-login counters for existing users, kept in the database (M1.8).

The in-memory :class:`app.core.auth.LoginRateLimiter` has to cap how many keys it
tracks, so with enough junk keys an attacker could push a near-locked username out of
it and reset its count. Counters for real accounts therefore live here instead: one
``login_failures`` row per failed attempt, with no cap on how many users are tracked.
Junk usernames have no account to protect and stay in the in-memory limiter, so a
spray of them never touches these rows. Lockouts also survive a restart.

**Counted up front.** :meth:`UserLoginFailures.acquire` inserts the attempt's row and
commits it *before* the slow password check, then counts the user's rows in the window.
More than ``limit`` means the attempt is refused, and its own row is deleted (a refused
attempt counts nothing, as in the in-memory limiter). Why parallel guesses can't exceed
the limit: take the attempts that were let through; their rows are never deleted by a
refusal. The one whose row was committed last counted after its own commit, so it saw
every other let-through row (and the earlier ones). It was let through, so that total
was at most ``limit``. This needs only that a statement sees every row committed before
it started, which holds on SQLite (one writer at a time, reads see the latest commit)
and on Postgres at the default READ COMMITTED isolation level. A race can only make an
attempt see a row that is about to be deleted, which refuses it: the safe direction.

**Bounded.** Each write prunes the user's rows that have left the window, refused
attempts delete their own row, and let-through attempts stop at ``limit``. So a user has
at most ``limit`` rows in the window plus one per attempt still in flight.

Nothing secret is stored: a row is a user id and a time.
"""

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.db import utcnow
from app.models import LoginFailure


class UserLoginFailures:
    """Sliding-window failure counts per user id, in ``login_failures``."""

    def __init__(
        self,
        limit: int,
        window: timedelta = timedelta(minutes=15),
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        self._limit = limit
        self._window = window
        self._clock = clock

    def acquire(self, db: Session, user_id: int) -> int | None:
        """Count one attempt for ``user_id`` and commit it, before the password check.

        Returns ``None`` if the attempt may go ahead (it stays counted as a failure until
        :meth:`clear` runs after a successful login), or seconds to wait if the user is
        locked (and then nothing stays counted). Commits ``db``: call it before the
        request has other pending changes.
        """
        now = self._clock()
        cutoff = now - self._window
        db.execute(
            delete(LoginFailure)
            .where(LoginFailure.user_id == user_id, LoginFailure.at <= cutoff)
            .execution_options(synchronize_session=False)
        )
        row = LoginFailure(user_id=user_id, at=now)
        db.add(row)
        try:
            db.commit()
        except IntegrityError:
            # The user was deleted since it was looked up: there is no account to count
            # for. The caller's password check then fails as for any missing user.
            db.rollback()
            return None
        # A new statement, after the commit: it sees every attempt committed before it.
        recent = db.execute(
            select(LoginFailure.id, LoginFailure.at)
            .where(LoginFailure.user_id == user_id, LoginFailure.at > cutoff)
            .order_by(LoginFailure.at, LoginFailure.id)
        ).all()
        if len(recent) <= self._limit:
            return None
        db.execute(
            delete(LoginFailure)
            .where(LoginFailure.id == row.id)
            .execution_options(synchronize_session=False)
        )
        db.commit()
        others = [at for row_id, at in recent if row_id != row.id]
        # Locked until enough of the counted failures leave the window.
        unlock_at = others[-self._limit] + self._window
        wait = (unlock_at - now).total_seconds()
        return max(1, int(wait) + 1)

    @staticmethod
    def clear(db: Session, user_id: int) -> int:
        """Delete every counted failure for ``user_id``. Doesn't commit. Returns how many."""
        result = cast(
            "CursorResult[Any]",
            db.execute(
                delete(LoginFailure)
                .where(LoginFailure.user_id == user_id)
                .execution_options(synchronize_session=False)
            ),
        )
        return result.rowcount
