"""Vantage's self-monitoring (P0-4): system alerts and ``/healthz``.

Three failures would otherwise be invisible, because the alerting itself depends on them:

=====================  ========================================  ===============================
Failure                Detected by                               Threshold
=====================  ========================================  ===============================
Prometheus unreachable the evaluator's per-tick probe            2 min of failed probes in a row
                       (connection refused, timeout, HTTP 5xx)
Metrics not collected  each tick: the age of the newest           newest sample older than 2 min,
                       ``up{job="vantage-backend"} == 1``         or none for 2 min
Evaluation stopped     :class:`Watchdog`: the evaluator's last   no tick for 3 x the interval
                       finished tick                             (90 s); restarts it once
=====================  ========================================  ===============================

A rule's own bad PromQL or too-many-series never counts as "Prometheus unreachable": only
the probe (``vector(1)``) decides.

**System alerts** are ``alert_events`` rows with ``source = system``, no rule and a fixed
``system_kind``, at most one open per kind (a partial unique index). They go through the
outbox to every channel with ``system_alerts`` set, and need no Prometheus. The evaluator
folds the affected alerts' NO DATA messages into the system alert's messages (see
``app.alerts.evaluator``), so an outage sends one message, not one per alert.

**/healthz** answers 200, or 503 with exactly one reason code, chosen in this order (the
first that applies): ``evaluator_stalled`` (nothing else is being checked then),
``prometheus_unreachable``, ``metrics_not_collected``, ``notifications_stuck`` (a message
unfinished for more than 15 minutes, or failed within 24 hours: the UI banner's rule).
The conditions live in memory, so they start over on a restart.

Limitation: if the backend or its host is down, nothing here runs. An external monitor
must probe ``/healthz`` from another machine (docs/SECURITY.md).
"""

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.alerts.delivery import enqueue, stuck_count
from app.collectors.hub import EventHub, Message
from app.core.db import utcnow
from app.models import (
    OPEN_STATES,
    AlertEvent,
    AlertNotification,
    AlertState,
    Channel,
    EndReason,
    EventSource,
    NotificationKind,
    SystemKind,
)

log = logging.getLogger(__name__)

UNREACHABLE_AFTER = timedelta(minutes=2)
METRICS_STALE_AFTER = timedelta(minutes=2)
# The evaluator is stalled after this many intervals without a finished tick.
STALL_INTERVALS = 3
WATCHDOG_SECONDS = 15.0
# How long a cancelled, hung evaluator task gets to finish before it is abandoned.
RESTART_GRACE_SECONDS = 5.0
BACKEND_JOB = "vantage-backend"
# Seconds since Prometheus last scraped Vantage successfully (computed by Prometheus, so
# the two clocks needn't agree); empty when there is no recent successful scrape.
METRICS_QUERY = f'min(time() - timestamp(up{{job="{BACKEND_JOB}"}} == 1))'

TITLES = {
    SystemKind.PROMETHEUS_UNREACHABLE: "Prometheus unreachable",
    SystemKind.METRICS_NOT_COLLECTED: "Metrics not being collected",
    SystemKind.EVALUATOR_STALLED: "Alert evaluation stopped",
}
SYSTEM_SEVERITY = "critical"


class Health(StrEnum):
    """``/healthz`` reason codes. PR 10/11 add ``maintenance`` and ``key_mismatch``."""

    EVALUATOR_STALLED = "evaluator_stalled"
    PROMETHEUS_UNREACHABLE = "prometheus_unreachable"
    METRICS_NOT_COLLECTED = "metrics_not_collected"
    NOTIFICATIONS_STUCK = "notifications_stuck"


# When several apply, the first one is reported.
HEALTH_PRIORITY = (
    Health.EVALUATOR_STALLED,
    Health.PROMETHEUS_UNREACHABLE,
    Health.METRICS_NOT_COLLECTED,
    Health.NOTIFICATIONS_STUCK,
)


def hhmm(at: datetime) -> str:
    return at.strftime("%H:%M UTC")


def minutes(seconds: float) -> str:
    n = max(1, round(seconds / 60))
    return f"{n} min"


def plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def system_message(event: AlertEvent) -> Message:
    return {
        "type": "alert_event",
        "event_id": event.id,
        "rule_id": None,
        "instance_id": None,
        "fingerprint": event.fingerprint,
        "severity": event.severity,
        "state": event.state.value,
        "no_data_reason": None,
        "started_at": event.started_at.isoformat(),
        "state_since": event.state_since.isoformat() if event.state_since else None,
        "ended_at": event.ended_at.isoformat() if event.ended_at else None,
        "summary": event.summary,
        "source": EventSource.SYSTEM.value,
        "system_kind": event.system_kind,
    }


class SelfMonitor:
    """The self-check conditions (in memory) and the system alerts (in the database)."""

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        hub: EventHub,
        clock: Callable[[], datetime] = utcnow,
        interval: float = 30.0,
    ) -> None:
        self._sessions = session_factory
        self._hub = hub
        self.clock = clock
        self._interval = interval
        self.unreachable_since: datetime | None = None
        # Since when the probe answers: scraping can't be judged sooner than 2 minutes
        # after Prometheus (or Vantage) came up.
        self._reachable_since: datetime | None = None
        self.metrics_bad_since: datetime | None = None
        self._metrics_age: float | None = None
        self._metrics_known = False
        self._started_at: datetime | None = None
        self.last_tick: datetime | None = None
        # Set when the watchdog saw the evaluator stalled; cleared by its next tick.
        self.stalled_at: datetime | None = None

    # --- conditions ----------------------------------------------------------------------

    def evaluator_started(self, now: datetime) -> None:
        self._started_at = now

    def tick_finished(self, now: datetime) -> None:
        self.last_tick = now

    @property
    def stall_after(self) -> timedelta:
        return timedelta(seconds=STALL_INTERVALS * self._interval)

    def stalled(self, now: datetime) -> bool:
        """No tick finished for 3 intervals since the evaluator (re)started."""
        if self._started_at is None:
            return False
        latest = max(self._started_at, self.last_tick or self._started_at)
        return now - latest >= self.stall_after

    def record_probe(self, ok: bool, now: datetime) -> None:
        if ok:
            self.unreachable_since = None
            if self._reachable_since is None:
                self._reachable_since = now
        else:
            self._reachable_since = None
            if self.unreachable_since is None:
                self.unreachable_since = now

    def prometheus_down(self, now: datetime) -> bool:
        since = self.unreachable_since
        return since is not None and now - since >= UNREACHABLE_AFTER

    def record_metrics(self, age: float | None, now: datetime, *, known: bool = True) -> None:
        """``age``: seconds since Prometheus last scraped Vantage, None if no recent
        scrape. ``known=False``: the check itself failed; nothing changes."""
        if not known:
            return
        self._metrics_known = True
        self._metrics_age = age
        if age is not None and age <= METRICS_STALE_AFTER.total_seconds():
            self.metrics_bad_since = None
        elif self.metrics_bad_since is None:
            back = age if age is not None else 0.0
            self.metrics_bad_since = now - timedelta(seconds=back)

    def metrics_down(self, now: datetime) -> bool:
        reachable = self._reachable_since
        if not self._metrics_known or reachable is None or now - reachable < METRICS_STALE_AFTER:
            return False
        age = self._metrics_age
        if age is not None:
            return age > METRICS_STALE_AFTER.total_seconds()
        since = self.metrics_bad_since
        return since is not None and now - since >= METRICS_STALE_AFTER

    def metrics_ok(self) -> bool:
        """The last check saw a recent scrape (what ends the system alert)."""
        age = self._metrics_age
        return (
            self._metrics_known and age is not None and age <= METRICS_STALE_AFTER.total_seconds()
        )

    def health(self, now: datetime | None = None) -> Health | None:
        """The ``/healthz`` reason, None when healthy."""
        now = now or self.clock()
        active = {
            Health.EVALUATOR_STALLED: self.stalled(now) or self.stalled_at is not None,
            Health.PROMETHEUS_UNREACHABLE: self.prometheus_down(now),
            Health.METRICS_NOT_COLLECTED: self.metrics_down(now),
        }
        for reason in HEALTH_PRIORITY:
            if reason == Health.NOTIFICATIONS_STUCK:
                with self._sessions() as db:
                    if stuck_count(db, now) > 0:
                        return reason
            elif active.get(reason):
                return reason
        return None

    # --- system alerts -------------------------------------------------------------------

    @staticmethod
    def open_event(db: Session, kind: SystemKind) -> AlertEvent | None:
        return db.scalar(
            select(AlertEvent).where(
                AlertEvent.source == EventSource.SYSTEM,
                AlertEvent.system_kind == kind.value,
                AlertEvent.state.in_(OPEN_STATES),
            )
        )

    @staticmethod
    def carriers(db: Session, kind: SystemKind) -> dict[int, AlertNotification]:
        """The open system alert's FIRING messages by channel id: what the affected
        alerts' NO DATA messages fold into."""
        event = SelfMonitor.open_event(db, kind)
        if event is None:
            return {}
        rows = db.scalars(
            select(AlertNotification)
            .where(
                AlertNotification.event_id == event.id,
                AlertNotification.kind == NotificationKind.FIRING,
                AlertNotification.channel_id.is_not(None),
            )
            .order_by(AlertNotification.id)
        )
        return {r.channel_id: r for r in rows if r.channel_id is not None}

    @staticmethod
    def channels(db: Session) -> list[int]:
        return list(
            db.scalars(
                select(Channel.id).where(Channel.system_alerts.is_(True)).order_by(Channel.id)
            )
        )

    def open_alert(self, kind: SystemKind, detail: str, *, since: datetime) -> bool:
        """Open the system alert of ``kind`` (FIRING, ``started_at`` = ``since``) and queue
        its message to every channel with ``system_alerts``, in one transaction. False if
        one is already open."""
        now = self.clock()
        summary = f"{TITLES[kind]}: {detail}" if detail else TITLES[kind]
        with self._sessions() as db:
            if self.open_event(db, kind) is not None:
                return False
            event = AlertEvent(
                rule_id=None,
                source=EventSource.SYSTEM,
                system_kind=kind.value,
                fingerprint=kind.value,
                labels={},
                rule_name=TITLES[kind],
                severity=SYSTEM_SEVERITY,
                state=AlertState.FIRING,
                started_at=since,
                state_since=now,
                summary=summary[:300],
            )
            db.add(event)
            try:
                db.flush()
                enqueue(
                    db,
                    channel_ids=self.channels(db),
                    kind=NotificationKind.FIRING,
                    text=f"SYSTEM ALERT: {event.summary}",
                    event_id=event.id,
                    now=now,
                )
                db.commit()
            except IntegrityError:  # opened meanwhile by another writer
                db.rollback()
                return False
            self._hub.publish(system_message(event))
        log.warning("system alert firing: %s", kind.value)
        return True

    def resolve_alert(self, kind: SystemKind, detail: str) -> bool:
        """Resolve the open system alert of ``kind`` and queue its RESOLVED message.
        False if none is open."""
        now = self.clock()
        with self._sessions() as db:
            event = self.open_event(db, kind)
            if event is None:
                return False
            event.state = AlertState.RESOLVED
            event.ended_at = now
            event.end_reason = EndReason.RECOVERED
            text = f"RESOLVED: {detail}" if detail else f"RESOLVED: {TITLES[kind]}"
            enqueue(
                db,
                channel_ids=self.channels(db),
                kind=NotificationKind.RESOLVED,
                text=text,
                event_id=event.id,
                now=now,
            )
            db.commit()
            self._hub.publish(system_message(event))
        log.info("system alert resolved: %s", kind.value)
        return True


class Watchdog:
    """Checks every :data:`WATCHDOG_SECONDS` that the evaluator still finishes ticks.

    Stalled (no tick for 3 intervals, hung or crashed): the system alert "Alert evaluation
    stopped" fires and the evaluator task is restarted, once. The next finished tick
    resolves it and re-arms the restart. A tick blocked in synchronous code blocks the
    whole event loop, this watchdog included; only ``/healthz`` from outside sees that.
    """

    def __init__(
        self,
        monitor: SelfMonitor,
        restart: Callable[[], Awaitable[None]],
        *,
        check_seconds: float = WATCHDOG_SECONDS,
    ) -> None:
        self._monitor = monitor
        self._restart = restart
        self._check_seconds = check_seconds
        self._task: asyncio.Task[None] | None = None
        # A system alert may be open from before a restart: look once.
        self._maybe_open = True

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="vantage-alert-watchdog")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._check_seconds)
            try:
                await self.check()
            except Exception as exc:  # never let one bad check stop the watchdog
                log.warning("alert watchdog check failed (%s)", type(exc).__name__)

    async def check(self) -> None:
        monitor = self._monitor
        now = monitor.clock()
        if monitor.stalled_at is not None:
            last = monitor.last_tick
            if last is not None and last > monitor.stalled_at:  # ticking again
                monitor.stalled_at = None
                monitor.resolve_alert(
                    SystemKind.EVALUATOR_STALLED, "Alert evaluation running again"
                )
                self._maybe_open = False
            return
        if monitor.stalled(now):
            monitor.stalled_at = now
            since = monitor.last_tick or now
            monitor.open_alert(
                SystemKind.EVALUATOR_STALLED,
                f"no alert rule checked since {hhmm(since)}; restarting the evaluator",
                since=since,
            )
            self._maybe_open = True
            log.warning("alert evaluator stalled: restarting it")
            await self._restart()
            return
        if self._maybe_open:  # not stalled: close one left open (e.g. before a restart)
            monitor.resolve_alert(SystemKind.EVALUATOR_STALLED, "Alert evaluation running again")
            self._maybe_open = False
