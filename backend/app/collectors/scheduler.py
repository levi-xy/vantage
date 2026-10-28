"""The poll scheduler (M2.4): runs each enabled instance's ``poll()`` on its interval.

One asyncio task per polled instance, plus a supervisor that re-reads the instance
list every ``refresh_seconds`` and starts, restarts (after an edit) or stops tasks.
Only integrations that override ``Integration.poll`` are polled; scrape-only ones are
Prometheus' job (service discovery, M2.3).

Isolation: a poll that raises, hangs or returns garbage marks only its own instance
``down``; every other task keeps running. Error descriptions stored in the status and
written to the log never include exception messages, which can carry URLs, headers or
response bodies (and so secrets). Only the exception type, or the SSRF guard's own
message (host and reason), is kept, and decrypted secret values are scrubbed from it.
"""

import asyncio
import logging
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.collectors.store import MetricsStore, standard_labels
from app.core.crypto import DecryptionError, SecretBox
from app.core.db import utcnow
from app.core.net_guard import NetGuardError, PingError
from app.integrations import instance_config as ic
from app.integrations.base import InstanceContext, Integration, Sample
from app.integrations.registry import Registry
from app.models import Instance

log = logging.getLogger(__name__)

# An integration's poll_interval_seconds() is clamped to this range.
MIN_POLL_INTERVAL_SECONDS = 10.0
MAX_POLL_INTERVAL_SECONDS = 86_400.0
# A single poll is cut off after min(interval, this).
MAX_POLL_TIMEOUT_SECONDS = 60.0
# How often the instance list is re-read to pick up added, edited or removed instances.
REFRESH_SECONDS = 30.0

_SCRUBBED = "<redacted>"


def polls(cls: type[Integration]) -> bool:
    """Whether the integration has anything to poll (overrides the default no-op)."""
    return cls.poll is not Integration.poll


@dataclass(frozen=True)
class PollSpec:
    """What one poll task needs, read from an ``instances`` row."""

    instance_id: uuid.UUID
    type_id: str
    name: str
    site: str | None
    updated_at: datetime
    config: dict[str, Any] = field(repr=False)
    secrets: bytes | None = field(repr=False)

    @property
    def labels(self) -> dict[str, str]:
        return standard_labels(
            instance_id=self.instance_id, name=self.name, type_id=self.type_id, site=self.site
        )

    @property
    def fingerprint(self) -> tuple[str, str, str | None, datetime]:
        """Changes when the instance is edited, so its task restarts with the new config."""
        return (self.type_id, self.name, self.site, self.updated_at)


class _PollError(Exception):
    """A poll failure with a safe, secret-free description."""


@dataclass
class _Job:
    spec: PollSpec
    task: asyncio.Task[None]


class PollScheduler:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        registry: Registry,
        secret_box: SecretBox,
        net_guard_factory: ic.NetGuardFactory,
        store: MetricsStore,
        refresh_seconds: float = REFRESH_SECONDS,
        min_interval_seconds: float = MIN_POLL_INTERVAL_SECONDS,
    ) -> None:
        self._session_factory = session_factory
        self._registry = registry
        self._box = secret_box
        self._guards = net_guard_factory
        self._store = store
        self._refresh = refresh_seconds
        self._min_interval = min_interval_seconds
        self._jobs: dict[uuid.UUID, _Job] = {}
        self._supervisor: asyncio.Task[None] | None = None

    # --- lifecycle ----------------------------------------------------------------------

    def start(self) -> None:
        if self._supervisor is None:
            self._supervisor = asyncio.create_task(self._supervise(), name="vantage-poller")

    async def stop(self) -> None:
        tasks = [job.task for job in self._jobs.values()]
        if self._supervisor is not None:
            tasks.append(self._supervisor)
            self._supervisor = None
        self._jobs.clear()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def running(self) -> frozenset[uuid.UUID]:
        """Ids of instances with a poll task."""
        return frozenset(self._jobs)

    async def _supervise(self) -> None:
        while True:
            try:
                await self.sync()
            except Exception as exc:  # a DB hiccup must not stop polling for good
                log.warning("poll scheduler: reading instances failed (%s)", type(exc).__name__)
            await asyncio.sleep(self._refresh)

    # --- reconciling the task set -------------------------------------------------------

    async def sync(self) -> None:
        """Match the poll tasks to the enabled, pollable instances in the database."""
        specs = await asyncio.to_thread(self._load_specs)
        wanted = {spec.instance_id: spec for spec in specs}
        for instance_id in list(self._jobs):
            spec = wanted.get(instance_id)
            job = self._jobs[instance_id]
            if job.task.done():
                # Shouldn't happen (the loop catches everything), but a dead task must not
                # leave its instance unpolled for good: drop it so it is started again below.
                log.warning("poll task of instance %s had stopped; restarting", instance_id)
                del self._jobs[instance_id]
            elif spec is None or spec.fingerprint != job.spec.fingerprint:
                job.task.cancel()
                del self._jobs[instance_id]
        for instance_id in self._store.tracked() - wanted.keys():
            self._store.remove(instance_id)
        for instance_id, spec in wanted.items():
            if instance_id not in self._jobs:
                self._store.track(instance_id, spec.labels)
                task = asyncio.create_task(self._run(spec), name=f"vantage-poll-{instance_id}")
                self._jobs[instance_id] = _Job(spec=spec, task=task)

    def _load_specs(self) -> list[PollSpec]:
        with self._session_factory() as db:
            rows: Sequence[Instance] = db.scalars(
                select(Instance).where(Instance.enabled.is_(True))
            ).all()
            specs: list[PollSpec] = []
            for row in rows:
                cls = self._registry.get(row.type_id)
                if cls is None or not polls(cls):
                    continue
                specs.append(
                    PollSpec(
                        instance_id=row.id,
                        type_id=row.type_id,
                        name=row.name,
                        site=row.site,
                        updated_at=row.updated_at,
                        config=dict(row.config),
                        secrets=row.secrets,
                    )
                )
            return specs

    # --- polling ------------------------------------------------------------------------

    def interval_for(self, integration: Integration) -> float:
        try:
            raw = float(integration.poll_interval_seconds())
        except Exception:
            raw = self._min_interval
        if raw != raw:  # NaN
            raw = self._min_interval
        return min(max(raw, self._min_interval), MAX_POLL_INTERVAL_SECONDS)

    async def _run(self, spec: PollSpec) -> None:
        cls = self._registry.get(spec.type_id)
        if cls is None:  # removed from the registry since loading; nothing to do
            return
        integration = cls()
        interval = self.interval_for(integration)
        loop = asyncio.get_running_loop()
        while True:
            started = loop.time()
            try:
                await self.poll_once(spec, integration, interval=interval)
            except Exception as exc:  # poll_once doesn't raise; keep the loop alive anyway
                log.error("poll task of instance %s: %s", spec.instance_id, type(exc).__name__)
            await asyncio.sleep(max(0.0, interval - (loop.time() - started)))

    async def poll_once(
        self, spec: PollSpec, integration: Integration, *, interval: float | None = None
    ) -> None:
        """Run one poll and record the result. Never raises (except on cancellation)."""
        interval = self.interval_for(integration) if interval is None else interval
        timeout = min(interval, MAX_POLL_TIMEOUT_SECONDS)
        secrets: dict[str, str] = {}
        started = time.monotonic()
        try:
            secrets, ctx = self._context(type(integration), spec)
            samples = await self._poll(integration, ctx, timeout)
        except _PollError as exc:
            self._failed(spec, _scrub(str(exc), secrets), time.monotonic() - started)
            return
        except Exception as exc:  # last line of defence: never let one poll kill its task
            self._failed(spec, f"poll failed ({type(exc).__name__})", time.monotonic() - started)
            return
        duration = time.monotonic() - started
        was = self._store.status(spec.instance_id)
        try:
            dropped = self._store.record_success(
                spec.instance_id,
                spec.type_id,
                samples,
                at=utcnow(),
                duration_seconds=duration,
                forbidden=secrets.values(),
            )
        except Exception as exc:  # e.g. a sample the checks didn't anticipate
            self._failed(spec, f"storing poll results failed ({type(exc).__name__})", duration)
            return
        if dropped:
            log.warning(
                "poll of instance %s (%s) returned %d invalid samples; dropped",
                spec.instance_id,
                spec.type_id,
                dropped,
            )
        if was is not None and was.state == "down":
            log.info("instance %s (%s) is up again", spec.instance_id, spec.type_id)

    def _failed(self, spec: PollSpec, error: str, duration: float) -> None:
        was = self._store.status(spec.instance_id)
        self._store.record_failure(spec.instance_id, error, at=utcnow(), duration_seconds=duration)
        if was is None or was.state != "down" or was.error != error:
            log.warning(
                "poll of instance %s (%s) failed: %s", spec.instance_id, spec.type_id, error
            )

    def _context(
        self, cls: type[Integration], spec: PollSpec
    ) -> tuple[dict[str, str], InstanceContext]:
        try:
            secrets = ic.decrypt(self._box, spec.instance_id, spec.secrets)
        except DecryptionError:
            raise _PollError(
                "stored secrets can't be decrypted (was the master key changed?)"
            ) from None
        try:
            config: BaseModel = cls.config_schema.model_validate({**spec.config, **secrets})
        except ValidationError:
            # The error would echo the input, secrets included: say only that it's invalid.
            raise _PollError("stored config is not valid for this integration") from None
        try:
            guard = self._guards(cls)
        except Exception as exc:
            raise _PollError(f"network guard unavailable ({type(exc).__name__})") from None
        ctx = InstanceContext(
            instance_id=spec.instance_id,
            name=spec.name,
            config=config,
            labels=spec.labels,
            net_guard=guard,
        )
        return secrets, ctx

    @staticmethod
    async def _poll(
        integration: Integration, ctx: InstanceContext, limit_seconds: float
    ) -> list[Sample]:
        try:
            async with asyncio.timeout(limit_seconds):
                result = await integration.poll(ctx)
        except (TimeoutError, httpx.TimeoutException):
            raise _PollError("poll timed out") from None
        except NetGuardError as exc:  # names the host and the reason, nothing secret
            raise _PollError(f"refused by the network guard: {exc}") from None
        except PingError as exc:  # fixed text plus, at most, the checked address
            raise _PollError(f"ping failed: {exc}") from None
        except Exception as exc:
            raise _PollError(f"poll failed ({type(exc).__name__})") from None
        if not isinstance(result, list) or not all(isinstance(s, Sample) for s in result):
            raise _PollError("poll returned something other than a list of samples")
        return result


def _scrub(text: str, secrets: dict[str, str]) -> str:
    for value in sorted((v for v in secrets.values() if v), key=len, reverse=True):
        text = text.replace(value, _SCRUBBED)
    return text
