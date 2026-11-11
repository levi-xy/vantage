"""The maintenance lock and the backend heartbeat (P0-6): offline commands never run
next to a live backend.

Two ``settings`` rows, both runtime state (never in a backup, never restored):

* ``maintenance_lock`` = ``{operation, started_at, host, pid, token}``: held by an offline
  CLI command (``restore``, ``rotate-master-key``) from before its transaction until
  after it ends, success or failure. Taken with a plain INSERT, which fails while the
  row exists, so there is only ever one holder. A lock left by a killed command is
  removed only by ``app.cli maintenance-unlock``; it never expires on its own.
* ``backend_heartbeat`` = ``{state, at, host, pid, token}``: rewritten by the backend
  every :data:`HEARTBEAT_SECONDS` (``state`` ``running``), and once by the container's
  startup gate before migrations (``starting``). A clean shutdown deletes it.

The handshake: each side commits its own marker, then reads the other's. The CLI inserts
the lock, then refuses (and releases the lock) when the heartbeat is fresh; the backend
writes the heartbeat, then pauses (or, at startup, exits) when the lock is present.
Whichever order the writes and reads interleave in, at least one side sees the other's
marker, so the two never both go ahead. The marker is committed before the other side's
is read (not written and read in one transaction): on Postgres (READ COMMITTED) a read
inside the writer's own open transaction could miss the other side's marker while the
other side's read misses this uncommitted one.

Freshness compares wall clocks: the CLI and the backend run on the same host.
"""

import os
import secrets
import socket
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Engine, delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.db import utcnow
from app.core.setup_code import SETUP_CODE_KEY
from app.models import Setting

LOCK_KEY = "maintenance_lock"
HEARTBEAT_KEY = "backend_heartbeat"
# Runtime state, not data: left out of backups, kept (not replaced) by a restore. The
# one-time setup code (app.core.setup_code) is runtime state too.
RUNTIME_KEYS = frozenset({LOCK_KEY, HEARTBEAT_KEY, SETUP_CODE_KEY})

HEARTBEAT_SECONDS = 10.0
# A heartbeat younger than this means the backend runs (3 missed beats).
FRESH_FOR = timedelta(seconds=30)

STARTING = "starting"
RUNNING = "running"


@dataclass(frozen=True)
class Holder:
    """Who holds the maintenance lock (printed by the CLI and logged by the backend)."""

    operation: str
    started_at: str
    host: str
    pid: int | None

    @classmethod
    def from_value(cls, value: Any) -> "Holder":
        v = value if isinstance(value, dict) else {}
        pid = v.get("pid")
        return cls(
            operation=str(v.get("operation") or "unknown"),
            started_at=str(v.get("started_at") or "an unknown time"),
            host=str(v.get("host") or "unknown host"),
            pid=pid if isinstance(pid, int) and not isinstance(pid, bool) else None,
        )

    def describe(self) -> str:
        """``restore since 2026-11-02T10:00:00+00:00 (host, pid 42)``."""
        pid = f"pid {self.pid}" if self.pid is not None else "unknown pid"
        return f"{self.operation} since {self.started_at} ({self.host}, {pid})"


class LockHeld(Exception):
    def __init__(self, holder: Holder | None) -> None:
        super().__init__("maintenance lock held")
        self.holder = holder


class BackendRunning(Exception):
    def __init__(self, age_seconds: float) -> None:
        super().__init__("backend running")
        self.age_seconds = age_seconds


def marker(state_or_operation: str, *, field: str, now: datetime) -> dict[str, Any]:
    """A lock (``field="operation"``) or heartbeat (``field="state"``) value."""
    at = now.isoformat(timespec="seconds")
    return {
        field: state_or_operation,
        "started_at" if field == "operation" else "at": at,
        "host": socket.gethostname(),
        "pid": os.getpid(),
        # Not a secret: only lets a holder remove its own row and nobody else's.
        "token": secrets.token_hex(16),
    }


def _get(engine: Engine, key: str) -> Any:
    with Session(engine) as db:
        return db.scalar(select(Setting.value).where(Setting.key == key))


def read_lock(engine: Engine) -> Holder | None:
    with Session(engine) as db:
        row = db.get(Setting, LOCK_KEY)
        return None if row is None else Holder.from_value(row.value)


def heartbeat_age(value: Any, now: datetime) -> float | None:
    """Seconds since the heartbeat was written; None if it can't be read."""
    if not isinstance(value, dict) or not isinstance(value.get("at"), str):
        return None
    try:
        at = datetime.fromisoformat(value["at"])
    except ValueError:
        return None
    if at.tzinfo is None:
        at = at.replace(tzinfo=UTC)
    return (now - at).total_seconds()


def is_fresh(value: Any, now: datetime) -> bool:
    age = heartbeat_age(value, now)
    # Also a heartbeat from the "future" (the clock was set back): the backend runs.
    return age is not None and abs(age) < FRESH_FOR.total_seconds()


def _delete_own(engine: Engine, key: str, token: str) -> bool:
    """Delete the row if it is still the one with ``token``."""
    with Session(engine) as db:
        row = db.get(Setting, key)
        if row is None or not isinstance(row.value, dict) or row.value.get("token") != token:
            return False
        db.delete(row)
        db.commit()
        return True


# --- the CLI side ------------------------------------------------------------------------


def acquire(
    engine: Engine,
    operation: str,
    *,
    clock: Callable[[], datetime] = utcnow,
    after_write: Callable[[], None] | None = None,
) -> str:
    """Take the lock, then check the heartbeat. Returns the lock's token.

    Raises :class:`LockHeld` (someone else holds it) or :class:`BackendRunning` (the
    heartbeat is fresh; the lock is released again). ``after_write``: tests only.
    """
    value = marker(operation, field="operation", now=clock())
    token: str = value["token"]
    with Session(engine) as db:
        db.add(Setting(key=LOCK_KEY, value=value))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise LockHeld(read_lock(engine)) from None
    try:
        if after_write is not None:
            after_write()
        beat = _get(engine, HEARTBEAT_KEY)
        if is_fresh(beat, clock()):
            raise BackendRunning(heartbeat_age(beat, clock()) or 0.0)
    except BaseException:
        _delete_own(engine, LOCK_KEY, token)
        raise
    return token


def release(engine: Engine, token: str) -> None:
    _delete_own(engine, LOCK_KEY, token)


@contextmanager
def held(engine: Engine, operation: str) -> Iterator[None]:
    """Hold the lock for the body: released after it, success or failure."""
    token = acquire(engine, operation)
    try:
        yield
    finally:
        release(engine, token)


def unlock(engine: Engine) -> Holder | None:
    """Remove the lock whoever holds it (``app.cli maintenance-unlock``)."""
    with Session(engine) as db:
        row = db.get(Setting, LOCK_KEY)
        if row is None:
            return None
        holder = Holder.from_value(row.value)
        db.execute(delete(Setting).where(Setting.key == LOCK_KEY))
        db.commit()
        return holder


# --- the backend side --------------------------------------------------------------------


def write_heartbeat(engine: Engine, value: dict[str, Any]) -> None:
    with Session(engine) as db:
        row = db.get(Setting, HEARTBEAT_KEY)
        if row is None:
            db.add(Setting(key=HEARTBEAT_KEY, value=value))
        else:
            row.value = value
        db.commit()


def beat(
    engine: Engine, value: dict[str, Any], *, after_write: Callable[[], None] | None = None
) -> Holder | None:
    """Write the heartbeat, then read the lock: the lock's holder, or None."""
    write_heartbeat(engine, value)
    if after_write is not None:
        after_write()
    return read_lock(engine)


def delete_heartbeat(engine: Engine, token: str) -> bool:
    """A clean shutdown: delete the heartbeat if it is still this process's."""
    return _delete_own(engine, HEARTBEAT_KEY, token)


def startup_gate(
    engine: Engine,
    *,
    clock: Callable[[], datetime] = utcnow,
    after_write: Callable[[], None] | None = None,
) -> Holder | None:
    """The container's first step: heartbeat ``starting``, then the lock. With a lock the
    heartbeat is removed again (the backend doesn't start) and the holder returned."""
    value = marker(STARTING, field="state", now=clock())
    holder = beat(engine, value, after_write=after_write)
    if holder is not None:
        delete_heartbeat(engine, value["token"])
    return holder
