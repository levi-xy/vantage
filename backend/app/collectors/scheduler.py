"""The poll scheduler (M2.4): runs each enabled instance's ``poll()`` on its interval.

One asyncio task per polled instance, plus a supervisor that re-reads the instance
list every ``refresh_seconds`` and starts, restarts (after an edit) or stops tasks.
Only integrations that override ``Integration.poll`` are polled; scrape-only ones are
Prometheus' job (service discovery, M2.3).

Poll cycles (P0-7): every interval starts one cycle on a fixed schedule. A cycle makes
one attempt (one ``poll()`` call, cut off after ``min(interval, 60 s)``) and, after a
fast failure, at most one retry 10 s later, cut off 1 s before the next cycle. There is
no retry after a whole-attempt timeout, a ``net_guard`` refusal or an unreadable stored
config, nor when less than 20 s remain before the next cycle. The store counts failed
cycles: the instance goes ``down`` only after N of them in a row.

Isolation: a poll that raises, hangs or returns garbage affects only its own instance;
every other task keeps running. Error descriptions stored in the status and
written to the log never include exception messages, which can carry URLs, headers or
response bodies (and so secrets). Only the exception type, the SSRF guard's or ping
client's own message (host and reason), or the message of a ``PollFailure`` (written by
the integration to be shown) is kept, and decrypted secret values are scrubbed from it.
"""

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.collectors.hub import EventHub, refresh_message, status_message
from app.collectors.store import InstanceStatus, MetricsStore, standard_labels
from app.core.crypto import DecryptionError, SecretBox
from app.core.db import utcnow
from app.core.net_guard import NetGuardError, PingError
from app.integrations import instance_config as ic
from app.integrations.base import InstanceContext, Integration, Sample
from app.integrations.errors import PollFailure
from app.integrations.registry import Registry
from app.models import Instance

log = logging.getLogger(__name__)

# An integration's poll_interval_seconds() is clamped to this range.
MIN_POLL_INTERVAL_SECONDS = 10.0
MAX_POLL_INTERVAL_SECONDS = 86_400.0
# A single poll is cut off after min(interval, this).
MAX_POLL_TIMEOUT_SECONDS = 60.0
# A fast failure is retried once, this long after it, within the same cycle...
RETRY_DELAY_SECONDS = 10.0
# ...only when at least this long is left before the next cycle starts...
RETRY_MIN_REMAINING_SECONDS = 20.0
# ...and the retry is cut off this long before the next cycle starts.
RETRY_CUTOFF_SECONDS = 1.0
# How often the instance list is re-read to pick up added, edited or removed instances.
REFRESH_SECONDS = 30.0

_SCRUBBED = "<redacted>"


def instance_context(
    cls: type[Integration],
    spec: "PollSpec",
    box: SecretBox,
    guards: ic.NetGuardFactory,
) -> tuple[dict[str, str], InstanceContext]:
    """The decrypted secrets and the guarded context for one instance.

    Raises :class:`_PollError` with a secret-free description when that can't be done.
    """
    try:
        secrets = ic.decrypt(box, spec.instance_id, spec.secrets)
    except DecryptionError:
        raise _PollError(
            "stored secrets can't be decrypted (was the master key changed?)", retry=False
        ) from None
    try:
        config: BaseModel = cls.config_schema.model_validate({**spec.config, **secrets})
    except ValidationError as exc:
        # The error would echo the input, secrets included: only a config hint's fixed
        # text is shown, else that it's invalid.
        raise _PollError(ic.stored_config_error(exc), retry=False) from None
    try:
        guard = guards(cls)
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


# Longest PollFailure message kept in the status; the rest is cut off.
MAX_FAILURE_TEXT = 200


class _PollError(Exception):
    """A poll failure with a safe, secret-free description.

    ``limit`` caps the text, applied only after the secrets are scrubbed out of it: a
    cut first could leave a fragment of a secret that the scrub no longer matches.
    """

    def __init__(self, text: str, *, limit: int | None = None, retry: bool = True) -> None:
        super().__init__(text)
        self.limit = limit
        self.retry = retry


@dataclass(frozen=True)
class _Attempt:
    """The outcome of one ``poll()`` call: samples, or a safe error text."""

    duration: float
    samples: list[Sample] | None = None
    secrets: dict[str, str] = field(default_factory=dict, repr=False)
    error: str | None = None
    retry: bool = False


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
        hub: EventHub | None = None,
        refresh_seconds: float = REFRESH_SECONDS,
        min_interval_seconds: float = MIN_POLL_INTERVAL_SECONDS,
        max_poll_timeout_seconds: float = MAX_POLL_TIMEOUT_SECONDS,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._session_factory = session_factory
        self._registry = registry
        self._box = secret_box
        self._guards = net_guard_factory
        self._store = store
        self._hub = hub
        self._refresh = refresh_seconds
        self._min_interval = min_interval_seconds
        self._max_timeout = max_poll_timeout_seconds
        # Tests pass a fake clock and sleep; production uses the event loop's clock.
        self._clock = clock or (lambda: asyncio.get_running_loop().time())
        self._sleep = sleep
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

    def interval_for_type(self, type_id: str) -> float | None:
        """The poll interval of an integration type, None if it isn't polled (the alert
        evaluator's freshness window, P0-3b)."""
        cls = self._registry.get(type_id)
        if cls is None or not polls(cls):
            return None
        try:
            integration = cls()
        except Exception:
            return None
        return self.interval_for(integration)

    async def _run(self, spec: PollSpec) -> None:
        cls = self._registry.get(spec.type_id)
        if cls is None:  # removed from the registry since loading; nothing to do
            return
        integration = cls()
        interval = self.interval_for(integration)
        # Fixed schedule: cycles start on planned slots one interval apart, so neither a
        # retry (it ends before the next slot) nor a late wake-up of the event loop shifts
        # them. A cycle that ran past its slot is followed by the next one right away.
        next_start = self._clock()
        while True:
            next_start = max(next_start + interval, self._clock())
            try:
                await self.poll_once(spec, integration, interval=interval, next_start=next_start)
            except Exception as exc:  # poll_once doesn't raise; keep the loop alive anyway
                log.error("poll task of instance %s: %s", spec.instance_id, type(exc).__name__)
            await self._sleep(max(0.0, next_start - self._clock()))

    async def poll_once(
        self,
        spec: PollSpec,
        integration: Integration,
        *,
        interval: float | None = None,
        next_start: float | None = None,
    ) -> None:
        """Run one poll cycle (an attempt, maybe a retry) and record its result.

        ``next_start`` is when the next cycle starts (clock time); by default one interval
        from now. Never raises (except on cancellation).
        """
        interval = self.interval_for(integration) if interval is None else interval
        next_start = self._clock() + interval if next_start is None else next_start
        timeout = min(interval, self._max_timeout)
        attempt = await self._attempt(spec, integration, timeout)
        if (
            attempt.error is not None
            and attempt.retry
            and next_start - self._clock() >= RETRY_MIN_REMAINING_SECONDS
        ):
            await self._sleep(RETRY_DELAY_SECONDS)
            limit = min(timeout, next_start - RETRY_CUTOFF_SECONDS - self._clock())
            if limit > 0:
                attempt = await self._attempt(spec, integration, limit)
        if attempt.error is not None or attempt.samples is None:
            self._failed(spec, attempt.error or "poll failed", attempt.duration)
            return
        was = self._store.status(spec.instance_id)
        try:
            dropped = self._store.record_success(
                spec.instance_id,
                spec.type_id,
                attempt.samples,
                at=utcnow(),
                duration_seconds=attempt.duration,
                forbidden=attempt.secrets.values(),
            )
        except Exception as exc:  # e.g. a sample the checks didn't anticipate
            self._failed(
                spec, f"storing poll results failed ({type(exc).__name__})", attempt.duration
            )
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
        self._publish(spec, was, refresh=True)

    async def _attempt(
        self, spec: PollSpec, integration: Integration, limit_seconds: float
    ) -> _Attempt:
        """One ``poll()`` call. Never raises (except on cancellation)."""
        secrets: dict[str, str] = {}
        started = self._clock()
        try:
            secrets, ctx = self._context(type(integration), spec)
            samples = await self._poll(integration, ctx, limit_seconds)
        except _PollError as exc:
            text = _scrub(str(exc), secrets)  # scrub first, then cap
            if exc.limit is not None:
                text = text[: exc.limit]
            return _Attempt(duration=self._clock() - started, error=text, retry=exc.retry)
        except Exception as exc:  # last line of defence: never let one poll kill its task
            return _Attempt(
                duration=self._clock() - started,
                error=f"poll failed ({type(exc).__name__})",
                retry=True,
            )
        return _Attempt(duration=self._clock() - started, samples=samples, secrets=secrets)

    def _failed(self, spec: PollSpec, error: str, duration: float) -> None:
        """Record a failed cycle (the store decides when that makes the instance down)."""
        was = self._store.status(spec.instance_id)
        self._store.record_failure(spec.instance_id, error, at=utcnow(), duration_seconds=duration)
        now = self._store.status(spec.instance_id)
        if was is None or was.state != "down" or was.error != error:
            log.warning(
                "poll of instance %s (%s) failed (%d cycle(s) in a row): %s",
                spec.instance_id,
                spec.type_id,
                self._store.failed_cycles(spec.instance_id),
                error,
            )
        if now is not None and now.state == "down" and (was is None or was.state != "down"):
            log.warning("instance %s (%s) is down", spec.instance_id, spec.type_id)
        self._publish(spec, was, refresh=False)

    def _publish(self, spec: PollSpec, was: InstanceStatus | None, *, refresh: bool) -> None:
        """Push the new status when it changed, and a refresh hint after a good poll."""
        if self._hub is None:
            return
        now = self._store.status(spec.instance_id)
        if now is not None and (was is None or (was.state, was.error) != (now.state, now.error)):
            self._hub.publish(status_message(spec.instance_id, now))
        if refresh:
            self._hub.publish(refresh_message(spec.instance_id))

    def _context(
        self, cls: type[Integration], spec: PollSpec
    ) -> tuple[dict[str, str], InstanceContext]:
        return instance_context(cls, spec, self._box, self._guards)

    @staticmethod
    async def _poll(
        integration: Integration, ctx: InstanceContext, limit_seconds: float
    ) -> list[Sample]:
        deadline = asyncio.timeout(limit_seconds)
        try:
            async with deadline:
                result = await integration.poll(ctx)
        except (TimeoutError, httpx.TimeoutException):
            # A whole-attempt timeout (the device hung) isn't retried; a timeout inside
            # the poll (one request got no reply) is.
            raise _PollError("poll timed out", retry=not deadline.expired()) from None
        except NetGuardError as exc:  # names the host and the reason, nothing secret
            raise _PollError(f"refused by the network guard: {exc}", retry=False) from None
        except PingError as exc:  # fixed text plus, at most, the checked address
            raise _PollError(f"ping failed: {exc}") from None
        except PollFailure as exc:  # a message the integration wrote to be shown
            # Capped by poll_once, after the scrub, never here.
            text = str(exc) or f"poll failed ({type(exc).__name__})"
            raise _PollError(text, limit=MAX_FAILURE_TEXT) from None
        except Exception as exc:
            raise _PollError(f"poll failed ({type(exc).__name__})") from None
        if not isinstance(result, list) or not all(isinstance(s, Sample) for s in result):
            raise _PollError("poll returned something other than a list of samples")
        return result


def _scrub(text: str, secrets: dict[str, str]) -> str:
    for value in sorted((v for v in secrets.values() if v), key=len, reverse=True):
        text = text.replace(value, _SCRUBBED)
    return text
