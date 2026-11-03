"""key_mismatch mode (P0-6): what the backend does while the master key doesn't match.

At startup, and then every :data:`RECHECK_SECONDS`, the key monitor decrypts the
``key_check`` value (``app.core.key_check``; on an upgrade it creates it). When that
fails the backend keeps running, so reads, logins and the UI still work and the admin can
see what is wrong, but:

* polling, live rates and notification delivery stop (they would only fail on secrets
  they can't decrypt, and delivery would mark every queued message failed for good);
* every secret write is refused with 503 ``key_mismatch`` (each write checks again in its
  own transaction, ``app.core.key_check.require``);
* ``/healthz`` answers 503 ``key_mismatch`` (first in its order);
* an ERROR line is logged at once and repeated every 5 minutes (per-table counts only);
* a system alert ``key_mismatch`` is stored; its messages are recorded ``failed`` with
  ``error_kind = credentials_unreadable`` (the channels' credentials can't be decrypted
  either), and the UI shows it in the system-alert banner.

The first check that matches again (the database changed: a restore, a rotation; or the
backend restarted with the right key file) resolves the system alert and starts polling
and delivery again. Reporting never depends on a channel working.
"""

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

from sqlalchemy.orm import Session, sessionmaker

from app.alerts.selfcheck import SelfMonitor
from app.core import key_check
from app.core.crypto import SecretBox
from app.core.db import utcnow
from app.core.key_check import Mismatch
from app.models import SystemKind

log = logging.getLogger(__name__)

RECHECK_SECONDS = 15.0
ERROR_REPEAT = timedelta(minutes=5)
UNDELIVERABLE = "credentials_unreadable"

_ERROR_LINE = (
    "master key mismatch (%s): the running master key doesn't match the stored secrets; "
    "polling, notifications and secret writes are stopped. Fix the key file and restart "
    "the backend (docs/SECURITY.md, 'Master key mismatch')"
)


class KeyMonitor:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        secret_box: SecretBox,
        monitor: SelfMonitor,
        pause: Callable[[], Awaitable[None]],
        resume: Callable[[], Awaitable[None]],
        clock: Callable[[], datetime] = utcnow,
        recheck_seconds: float = RECHECK_SECONDS,
    ) -> None:
        self._sessions = session_factory
        self._box = secret_box
        self._monitor = monitor
        self._pause = pause
        self._resume = resume
        self._clock = clock
        self._recheck = recheck_seconds
        self.mismatch: Mismatch | None = None
        self._last_error: datetime | None = None
        # A system alert may be open from before a restart: resolve it on the first match.
        self._maybe_open = True
        self._task: asyncio.Task[None] | None = None

    @property
    def ok(self) -> bool:
        return self.mismatch is None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="vantage-key-check")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._recheck)
            try:
                await self.check()
            except Exception as exc:  # a DB hiccup must not stop the re-checks
                log.warning("master key check failed (%s)", type(exc).__name__)

    def _check_db(self) -> key_check.KeyCheck:
        with self._sessions() as db:
            result = key_check.check(db, self._box, create=True)
            db.commit()
        return result

    async def check(self) -> bool:
        """Check once and switch modes when the answer changed. Returns whether the key
        matches."""
        # The database read and the decrypts run off the event loop.
        result = await asyncio.to_thread(self._check_db)
        if result.created:
            log.info("master key check value created (%s)", result.describe() or "no secrets")
        now = self._clock()
        if result.mismatch is None:
            was = self.mismatch
            self.mismatch = None
            self._monitor.key_mismatch = False
            self._last_error = None
            if was is not None:
                log.warning("master key matches the stored secrets again: resuming")
                await self._resume()
            if self._maybe_open:
                self._monitor.resolve_alert(
                    SystemKind.KEY_MISMATCH, "Master key matches the stored secrets again"
                )
                self._maybe_open = False
            return True

        first = self.mismatch is None
        self.mismatch = result.mismatch
        self._monitor.key_mismatch = True
        self._maybe_open = True
        if first:
            await self._pause()
            if result.counts:
                log.error("master key mismatch: stored secrets %s", result.describe())
            self._monitor.open_alert(
                SystemKind.KEY_MISMATCH,
                f"the master key doesn't match the stored secrets ({result.mismatch.value}); "
                "polling and notifications are stopped",
                since=now,
                undeliverable=UNDELIVERABLE,
            )
        if self._last_error is None or now - self._last_error >= ERROR_REPEAT:
            log.error(_ERROR_LINE, result.mismatch.value)
            self._last_error = now
        return False
