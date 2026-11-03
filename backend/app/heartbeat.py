"""The backend's heartbeat and maintenance mode (P0-6, ``app.core.maintenance``).

Every :data:`~app.core.maintenance.HEARTBEAT_SECONDS` the backend rewrites the
``backend_heartbeat`` row, then reads the ``maintenance_lock`` row. A lock present means
an offline command (restore, master-key rotation) runs or was interrupted: the backend
goes into **maintenance mode** until the lock is gone. Polling, live rates, alert
evaluation and notification delivery pause, state-changing API requests answer 503
``maintenance`` (reads and login keep working) and ``/healthz`` answers ``maintenance``.

A clean shutdown deletes the heartbeat, so the CLI can run as soon as the backend stops.
"""

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime

from sqlalchemy import Engine

from app.core import maintenance
from app.core.db import utcnow
from app.core.maintenance import Holder

log = logging.getLogger(__name__)


class Heartbeat:
    def __init__(
        self,
        engine: Engine,
        on_change: Callable[[bool], Awaitable[None]],
        *,
        interval: float = maintenance.HEARTBEAT_SECONDS,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._engine = engine
        self._on_change = on_change
        self._interval = interval
        self._clock = clock
        self._token: str | None = None
        self.holder: Holder | None = None
        self._task: asyncio.Task[None] | None = None

    @property
    def maintenance(self) -> bool:
        return self.holder is not None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="vantage-heartbeat")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            try:
                await self.beat()
            except Exception as exc:  # a DB hiccup must not stop the heartbeat
                log.warning("heartbeat failed (%s)", type(exc).__name__)

    async def beat(self) -> bool:
        """Write the heartbeat, read the lock, switch modes if that changed. Returns
        whether the backend is in maintenance mode."""
        value = maintenance.marker(maintenance.RUNNING, field="state", now=self._clock())
        if self._token is not None:
            value["token"] = self._token
        self._token = value["token"]
        holder = await asyncio.to_thread(maintenance.beat, self._engine, value)
        was = self.holder
        self.holder = holder
        if holder is not None and was is None:
            log.warning(
                "maintenance in progress: %s. Polling, alert evaluation and notifications "
                "are paused and changes are refused until it finishes",
                holder.describe(),
            )
            await self._on_change(True)
        elif holder is None and was is not None:
            log.warning("maintenance lock gone: resuming")
            await self._on_change(False)
        return holder is not None

    async def shutdown(self) -> None:
        """Delete this process's heartbeat (a clean shutdown)."""
        await self.stop()
        if self._token is None:
            return
        try:
            await asyncio.to_thread(maintenance.delete_heartbeat, self._engine, self._token)
        except Exception as exc:
            log.warning("could not remove the heartbeat at shutdown (%s)", type(exc).__name__)
