"""Alert rule evaluator (M7.1; one alert per series since P0-3a; state machine P0-3b).

Every ``interval`` seconds the evaluator runs, through a guarded Prometheus client of its
own (``app.query.proxy``, same budget as the browser's queries, but a separate connection
pool and outside the ``/query`` concurrency caps, so browser queries can't starve it):

1. a probe (``vector(1)``). If it fails, the tick changes no state: open alerts stay as
   they are, pending timers pause, nothing counts (after 2 minutes of failures, see
   "Self-monitoring" below);
2. two fleet freshness queries: the last successful poll per polled device
   (``vantage_poll_last_success_timestamp_seconds``) and the last ``up == 1`` scrape per
   scraped target;
3. per enabled rule: the rule's query, then (for a *value* rule, see ``app.alerts.promql``)
   its witness queries, scoped like the rule (in PromQL, :func:`scoped_query`). Rules run
   side by side with at most 4 queries in flight; the witness and fleet queries may
   return up to 10,000 series (the browser's budget is 500).

Each series of a rule's result is its own alert, identified by (rule id, fingerprint) (see
``app.alerts.labels``). The transitions are pure functions in ``app.alerts.state``; this
module does only the I/O around them. In short: a series fires once it has been in the
result for ``for_seconds`` (gaps without data pause the timer); an open alert resolves only
after two clean checks on two different polls (a fresh value seen, condition false); a
missing value, a stale source or a failing query puts it into NO DATA instead, which stays
open. Every message is queued in the ``alert_notifications`` outbox in the same transaction
as the event change (P0-5).

Freshness: a polled device is fresh while its last successful poll is younger than
2 x its poll interval + 30 s (the scheduler's interval for its integration); a scraped
target while its last ``up == 1`` scrape is younger than 90 s. A series without
``vantage_instance_id`` (an aggregate) has no source check.

In memory only: pending timers, RECOVERING counts, the last source observation each alert
used, query-failure streaks and each rule's last query error (shown by the API). Open
events are re-read from the database on every tick, so a restart neither duplicates nor
forgets an alert; it starts the in-memory parts over, and sources stay stale until their
first poll after the restart, so an alert may enter NO DATA quietly (its message waits
5 minutes) but never resolves on the restart itself.

Rule edits close events in the API, in the same transaction as the edit, and bump the
rule's ``revision``. When the evaluator sees a new revision it drops that rule's RECOVERING
counts and witness state, and, when the expression differs, its pending timers; it never
opens an event for a rule whose revision moved while its query ran.

An open event from before migration 0006 (fingerprint ``legacy``) is adopted by the series
if exactly one series has no event yet; with several it is resolved quietly
(``rule_changed``); with none it stays open (an empty result is no evidence of recovery)
until a series appears or an admin closes it.

Self-monitoring and shared failures (P0-4, ``app.alerts.selfcheck``): every tick records
the probe's outcome and, when it answers, how old Prometheus' newest scrape of Vantage is.

* Prometheus unreachable for 2 minutes: the system alert opens ("7 open alerts and 12
  rules can't be checked") and every open alert goes into NO DATA
  (``prometheus_unreachable``), its NO DATA message stored ``folded`` into the system
  alert's message. Shorter gaps change nothing (pending timers pause). The first tick
  that reaches Prometheus again re-checks every alert as usual: still-true ones return to
  FIRING without a message, and the system alert's RESOLVED message counts them ("5 alerts
  still firing, 2 recovering").
* Metrics not collected (the newest scrape older than 2 minutes, or none for 2 minutes):
  the same for the alerts on polled devices (``metrics_not_collected``); their values in
  Prometheus are old, so nothing counts for them until scraping resumes.
* A device down: when its instance-down rule (``promql.is_instance_down``) fires, the
  device's other alerts already in NO DATA are folded into that message ("3 other alerts on
  this device can't be checked"); one entering NO DATA later is folded into a short
  follow-up message to the same channel (the FIRING message is usually sent by then). On a
  channel the instance-down rule doesn't use, and without such a rule, they are sent
  normally.
* While a device is in a failure streak (failed poll cycles, not yet down), its alerts'
  NO DATA messages are held, so a device about to be marked down doesn't first send one
  per alert.
* A rule whose query fails 3 ticks in a row shows the error (the rule API's ``error``);
  that is a configuration problem and sends no message.

Messages carry ids, numbers and a short summary built from the event's stored (filtered,
cleaned, capped) labels. Prometheus' error text is never logged or stored (the proxy only
passes on fixed messages).
"""

import asyncio
import contextlib
import logging
import math
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.alerts import labels as alert_labels
from app.alerts import promql
from app.alerts import state as sm
from app.alerts.delivery import enqueue, fold_into
from app.alerts.labels import INSTANCE_ID_LABEL, INSTANCE_NAME_LABEL, LEGACY_FINGERPRINT, REDACTED
from app.alerts.selfcheck import (
    METRICS_QUERY,
    RESTART_GRACE_SECONDS,
    SelfMonitor,
    hhmm,
    minutes,
    plural,
)
from app.collectors.hub import EventHub, Message
from app.collectors.store import LAST_SUCCESS
from app.core.db import utcnow
from app.models import (
    OPEN_STATES,
    AlertEvent,
    AlertNotification,
    AlertRule,
    AlertState,
    EndReason,
    EventSource,
    Instance,
    NotificationKind,
    NotificationStatus,
    SystemKind,
)
from app.query.proxy import PrometheusClient, QueryError, QueryRequest, QueryResult, Series

log = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECONDS = 30.0
DEFAULT_POLL_INTERVAL_SECONDS = 60.0
SCRAPE_INTERVAL_SECONDS = 30.0
MAX_PART_LENGTH = 48
MAX_SUMMARY_LENGTH = 300
MAX_ERROR_LENGTH = 120
# The witness and fleet queries are the backend's own: like seeding (collectors/seed.py)
# they may return more series than the browser's budget allows.
INTERNAL_MAX_SERIES = 10_000
# Rule and witness queries in flight at once.
MAX_CONCURRENT_QUERIES = 4
PROBE_QUERY = "vector(1)"
POLLED_QUERY = LAST_SUCCESS
_SCRAPED = f'up{{{INSTANCE_ID_LABEL}!=""}}'
# The scrape's timestamp where up is 1, 0 where it is 0 (vector matching ignores __name__).
SCRAPED_QUERY = f"timestamp({_SCRAPED}) * {_SCRAPED}"
# A series whose event ended for one of these reasons waits the full ``for`` again,
# counted from the close.
_RECOUNT_AFTER = (EndReason.CLOSED_BY_USER, EndReason.RULE_DISABLED, EndReason.RULE_CHANGED)

NO_DATA_WORDS = {
    sm.NoDataReason.SOURCE_STALE: "device not reporting",
    sm.NoDataReason.SOURCE_DOWN: "device not answering",
    sm.NoDataReason.METRIC_MISSING: "metric missing",
    sm.NoDataReason.QUERY_FAILED: "query failing",
    sm.NoDataReason.PROMETHEUS_UNREACHABLE: "Prometheus unreachable",
    sm.NoDataReason.METRICS_NOT_COLLECTED: "metrics not being collected",
}
# The system alert that speaks for each shared cause.
SHARED_KIND = {
    sm.NoDataReason.PROMETHEUS_UNREACHABLE: SystemKind.PROMETHEUS_UNREACHABLE,
    sm.NoDataReason.METRICS_NOT_COLLECTED: SystemKind.METRICS_NOT_COLLECTED,
}
# NO DATA reasons that mean "the device itself isn't answering".
DEVICE_REASONS = (sm.NoDataReason.SOURCE_DOWN.value, sm.NoDataReason.SOURCE_STALE.value)
NO_DATA_PREFIX = "NO DATA: "


def freshness_window(interval_seconds: float) -> float:
    """A source is fresh while its last observation is younger than this."""
    return 2 * interval_seconds + 30


def clean(text: str, limit: int = MAX_PART_LENGTH) -> str:
    """Printable text on one line, capped."""
    return alert_labels.clean(text, limit)


def normalize_expr(expr: str) -> str:
    """Whitespace-insensitive form: an edit that only reflows the PromQL isn't a change."""
    return " ".join(expr.split())


def describe(stored: dict[str, str]) -> str:
    """A short name for one alert, from its stored labels only."""
    name = stored.get(INSTANCE_NAME_LABEL, "")
    extra = [
        f"{k}={v}"
        for k, v in sorted(stored.items())
        if k not in (INSTANCE_ID_LABEL, INSTANCE_NAME_LABEL) and v and v != REDACTED
    ][:2]
    parts = ([clean(name)] if name else []) + [clean(e) for e in extra]
    return " ".join(parts) or "series"


def summarize(rule_name: str, severity: str, stored: dict[str, str]) -> str:
    return f"{clean(rule_name, 80)} ({severity}): {describe(stored)}"[:MAX_SUMMARY_LENGTH]


def event_message(event: AlertEvent) -> Message:
    return {
        "type": "alert_event",
        "event_id": event.id,
        "rule_id": event.rule_id,
        "instance_id": str(event.instance_id) if event.instance_id else None,
        "fingerprint": event.fingerprint,
        "severity": event.severity,
        "state": event.state.value,
        "no_data_reason": event.no_data_reason if event.state == AlertState.NO_DATA else None,
        "started_at": event.started_at.isoformat(),
        "state_since": event.state_since.isoformat() if event.state_since else None,
        "ended_at": event.ended_at.isoformat() if event.ended_at else None,
        "summary": event.summary,
        "source": EventSource.RULE.value,
        "system_kind": None,
    }


def in_scope(instance_id: uuid.UUID | None, labels: dict[str, str]) -> bool:
    """A rule tied to one instance only counts that instance's series."""
    if instance_id is None:
        return True
    return labels.get(INSTANCE_ID_LABEL) == str(instance_id)


def label_instance_id(db: Session, stored: dict[str, str]) -> uuid.UUID | None:
    """The device of a series, if its ``vantage_instance_id`` names an existing instance."""
    raw = stored.get(INSTANCE_ID_LABEL)
    if not raw:
        return None
    try:
        value = uuid.UUID(raw)
    except ValueError:
        return None
    return value if db.get(Instance, value) is not None else None


def close_events(
    db: Session, events: Sequence[AlertEvent], reason: EndReason, now: datetime
) -> list[AlertEvent]:
    """Resolve the still-open ones among ``events`` (no commit). Returns those closed."""
    closed = []
    for event in events:
        result = db.execute(
            update(AlertEvent)
            .where(AlertEvent.id == event.id, AlertEvent.state.in_(OPEN_STATES))
            .values(state=AlertState.RESOLVED, ended_at=now, end_reason=reason)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount:  # type: ignore[attr-defined]
            db.refresh(event)
            closed.append(event)
    return closed


def scoped_query(query: str, instance_id: uuid.UUID | None) -> str:
    """``query`` limited to one instance's series, for a rule scoped to it: Prometheus then
    returns only that device's series however big the fleet is. ``instance_id`` is a UUID
    from the database, never user text."""
    if instance_id is None:
        return query
    one = f'label_replace(vector(1), "{INSTANCE_ID_LABEL}", "{instance_id}", "", "")'
    return f"({query}) and on ({INSTANCE_ID_LABEL}) {one}"


def _value(series: Series) -> float | None:
    if not series.values:
        return None
    try:
        return float(series.values[-1][1])
    except ValueError:
        return None


@dataclass(frozen=True)
class _Seen:
    revision: int
    expr: str


@dataclass(frozen=True)
class _OpenRow:
    id: int
    fingerprint: str
    state: AlertState
    state_since: datetime
    no_data_reason: str | None
    reminder_sent_at: datetime | None
    labels: dict[str, str]


@dataclass
class _Fleet:
    """Last observation (Unix seconds) and freshness window per device id."""

    polled: dict[str, tuple[float, float]] = field(default_factory=dict)
    # Scraped: (last up == 1 timestamp or None, up right now).
    scraped: dict[str, tuple[float | None, bool]] = field(default_factory=dict)


def _reason(raw: str | None) -> sm.NoDataReason | None:
    try:
        return sm.NoDataReason(raw) if raw else None
    except ValueError:  # a reason a later version wrote
        return None


class AlertEvaluator:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        prometheus: Callable[[], PrometheusClient],
        hub: EventHub,
        interval: float = DEFAULT_INTERVAL_SECONDS,
        clock: Callable[[], datetime] = utcnow,
        poll_interval: Callable[[str], float | None] = lambda type_id: None,
        instance_state: Callable[[uuid.UUID], str | None] = lambda instance_id: None,
        failed_cycles: Callable[[uuid.UUID], int] = lambda instance_id: 0,
        monitor: SelfMonitor | None = None,
    ) -> None:
        self._sessions = session_factory
        self._prometheus = prometheus
        self._hub = hub
        self._interval = interval
        self._clock = clock
        # type_id -> the scheduler's poll interval (None: not polled / unknown).
        self._poll_interval = poll_interval
        # instance id -> the collector's state ("pending" / "up" / "down"), None if unknown.
        self._instance_state = instance_state
        # instance id -> failed poll cycles in a row (the collector's streak).
        self._failed_cycles = failed_cycles
        self.monitor = monitor or SelfMonitor(
            session_factory=session_factory, hub=hub, clock=clock, interval=interval
        )
        # Query failures in a row per rule: the error shows from the third (P0-4).
        self._fails: dict[int, int] = {}
        self._pending: dict[tuple[int, str], sm.Pending] = {}
        self._open: dict[tuple[int, str], sm.OpenAlert] = {}
        self._seen: dict[int, _Seen] = {}
        self._errors: dict[int, str] = {}
        # Scraped targets: the newest up == 1 scrape seen (survives an up == 0 tick).
        self._last_up: dict[str, float] = {}
        self._task: asyncio.Task[None] | None = None
        self._started_at: datetime | None = None
        self._slots: asyncio.Semaphore | None = None

    # --- lifecycle ---------------------------------------------------------------------

    def start(self) -> None:
        if self._task is None:
            self.monitor.evaluator_started(self._clock())
            self._task = asyncio.create_task(self._run(), name="vantage-alert-evaluator")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def restart(self) -> None:
        """Cancel the (hung or dead) loop and start a new one (the watchdog)."""
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            # A task stuck past cancellation is abandoned, not awaited forever.
            await asyncio.wait({task}, timeout=RESTART_GRACE_SECONDS)
        self._slots = None  # a cancelled query may still hold a slot
        self.start()

    async def _run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception as exc:  # never let one bad tick stop the loop
                log.warning("alert evaluation failed (%s)", type(exc).__name__)
            await asyncio.sleep(self._interval)

    # --- what the API shows ----------------------------------------------------------------

    def rule_error(self, rule_id: int) -> str | None:
        """The last query error seen for the rule (short, fixed text), None after a success."""
        return self._errors.get(rule_id)

    def set_rule_error(self, rule_id: int, message: str | None) -> None:
        if message:
            self._errors[rule_id] = clean(message, MAX_ERROR_LENGTH)
        else:
            self._errors.pop(rule_id, None)

    # --- one tick --------------------------------------------------------------------------

    async def tick(self) -> None:
        """One evaluation. It counts as finished (for the watchdog) when it returns or
        fails with an exception, never when it is cancelled: a hung tick the watchdog
        cancels must not look like a working evaluator."""
        try:
            await self._tick()
        except Exception:
            self.monitor.tick_finished(self._clock())
            raise
        self.monitor.tick_finished(self._clock())

    async def _tick(self) -> None:
        with self._sessions() as db:
            rules = list(
                db.scalars(
                    select(AlertRule).where(
                        AlertRule.enabled.is_(True), AlertRule.deleted_at.is_(None)
                    )
                )
            )
            rows = list(db.scalars(select(AlertEvent).where(AlertEvent.state.in_(OPEN_STATES))))
            db.expunge_all()
        system_open = {e.system_kind for e in rows if e.source == EventSource.SYSTEM}
        rows = [e for e in rows if e.source != EventSource.SYSTEM]
        active = {r.id for r in rules}
        open_by_rule: dict[int, dict[str, _OpenRow]] = {}
        orphans: list[int] = []
        for e in rows:
            if e.rule_id in active:
                open_by_rule.setdefault(e.rule_id, {})[e.fingerprint] = _OpenRow(
                    id=e.id,
                    fingerprint=e.fingerprint,
                    state=e.state,
                    state_since=e.state_since or e.started_at,
                    no_data_reason=e.no_data_reason,
                    reminder_sent_at=e.reminder_sent_at,
                    labels={str(k): str(v) for k, v in (e.labels or {}).items()},
                )
            else:
                orphans.append(e.id)
        if orphans:  # safety net: the API closes these when it disables or deletes a rule
            self._close_orphans(orphans)
        self._pending = {k: v for k, v in self._pending.items() if k[0] in active}
        self._open = {k: v for k, v in self._open.items() if k[1] in open_by_rule.get(k[0], {})}
        self._seen = {k: v for k, v in self._seen.items() if k in active}
        self._errors = {k: v for k, v in self._errors.items() if k in active}
        self._fails = {k: v for k, v in self._fails.items() if k in active}

        now = self._clock()
        if self._started_at is None:
            self._started_at = now
        probe_failure = await self._probe()
        probe_ok = probe_failure is None
        self.monitor.record_probe(probe_ok, now, probe_failure)
        if not probe_ok:
            # Prometheus is unreachable: no state changes, pending timers pause.
            obs = sm.Observation(now=now, probe_ok=False)
            for key, pending in list(self._pending.items()):
                rule = next((r for r in rules if r.id == key[0]), None)
                new, _ = sm.step_pending(pending, obs, rule.for_seconds if rule else 0)
                self._set_pending(key, new)
            if self.monitor.prometheus_down(now):  # 2 minutes and more
                await self._prometheus_down(rules, open_by_rule, system_open, now)
            return

        # Shared causes that end with this tick: counted once every alert is re-checked.
        ending: dict[SystemKind, list[tuple[int, str, int]]] = {}
        if SystemKind.PROMETHEUS_UNREACHABLE in system_open:
            ending[SystemKind.PROMETHEUS_UNREACHABLE] = []
        metrics_down = await self._check_metrics(open_by_rule, system_open, now)
        if self.monitor.metrics_ok() and SystemKind.METRICS_NOT_COLLECTED in system_open:
            ending[SystemKind.METRICS_NOT_COLLECTED] = []
        for kind, affected in ending.items():
            reason = next(r for r, k in SHARED_KIND.items() if k == kind)
            affected += [
                (rule_id, fp, row.id)
                for rule_id, by_fp in open_by_rule.items()
                for fp, row in by_fp.items()
                if row.no_data_reason == reason.value
            ]
        carriers = (
            self._carrier_channels(SystemKind.METRICS_NOT_COLLECTED) if metrics_down else set()
        )

        fleet = await self._fleet()
        for rule in rules:
            self._check_revision(rule)

        async def evaluate(rule: AlertRule) -> None:
            try:
                await self._evaluate(
                    rule, open_by_rule.get(rule.id, {}), fleet, metrics_down, carriers
                )
            except Exception as exc:
                log.warning("alert rule %d failed (%s)", rule.id, type(exc).__name__)

        # Rules run side by side; _query keeps at most MAX_CONCURRENT_QUERIES in flight.
        await asyncio.gather(*(evaluate(rule) for rule in rules))
        for kind, affected in ending.items():
            self._end_shared(kind, affected, now)

    # --- shared causes (P0-4) ----------------------------------------------------------------

    def _carrier_channels(self, kind: SystemKind) -> set[int]:
        """The channels the open system alert of ``kind`` was sent to."""
        with self._sessions() as db:
            return set(SelfMonitor.carriers(db, kind))

    @staticmethod
    def _shared_wait(rule: AlertRule, carriers: set[int]) -> timedelta:
        """A shared cause's NO DATA message is folded at once when the system alert went
        to every channel of the rule; otherwise it waits the usual 5 minutes and is sent on
        the channels the system alert didn't reach (folded on the others)."""
        if set(rule.channel_ids) <= carriers:
            return timedelta(0)
        return sm.NO_DATA_MESSAGE_AFTER

    async def _prometheus_down(
        self,
        rules: list[AlertRule],
        open_by_rule: dict[int, dict[str, _OpenRow]],
        system_open: set[str | None],
        now: datetime,
    ) -> None:
        """Prometheus unreachable for 2 minutes: one system alert, every open alert NO DATA
        with its message folded into it."""
        kind = SystemKind.PROMETHEUS_UNREACHABLE
        if kind not in system_open:
            alerts = sum(len(by_fp) for by_fp in open_by_rule.values())
            since = self.monitor.unreachable_since or now
            detail = (
                f"since {hhmm(since)}. {plural(alerts, 'open alert')} and "
                f"{plural(len(rules), 'rule')} can't be checked"
            )
            self.monitor.open_alert(kind, detail, since=since)
        carriers = self._carrier_channels(kind)
        for rule in rules:
            obs = sm.Observation(
                now=now,
                probe_ok=False,
                shared_cause=sm.NoDataReason.PROMETHEUS_UNREACHABLE,
                no_data_wait=self._shared_wait(rule, carriers),
            )
            for fp, row in open_by_rule.get(rule.id, {}).items():
                if fp != LEGACY_FINGERPRINT:
                    await self._step_open(rule, row, obs)

    async def _check_metrics(
        self,
        open_by_rule: dict[int, dict[str, _OpenRow]],
        system_open: set[str | None],
        now: datetime,
    ) -> bool:
        """Whether Prometheus has stopped collecting Vantage's metrics (2 minutes); opens
        the system alert then."""
        try:
            result = await self._query(METRICS_QUERY)
        except QueryError:
            self.monitor.record_metrics(None, now, known=False)
        else:
            ages = [v for v in (_value(s) for s in result.series) if v is not None]
            finite = [v for v in ages if math.isfinite(v)]
            self.monitor.record_metrics(min(finite) if finite else None, now)
        down = self.monitor.metrics_down(now)
        kind = SystemKind.METRICS_NOT_COLLECTED
        if down and kind not in system_open:
            alerts = sum(
                1
                for by_fp in open_by_rule.values()
                for row in by_fp.values()
                if self._polled(row.labels)
            )
            since = self.monitor.metrics_bad_since or now
            detail = (
                f"Prometheus hasn't collected Vantage's metrics since {hhmm(since)}. "
                f"{plural(alerts, 'open alert')} on polled devices can't be checked"
            )
            self.monitor.open_alert(kind, detail, since=since)
        return down

    def _end_shared(
        self, kind: SystemKind, affected: list[tuple[int, str, int]], now: datetime
    ) -> None:
        """Resolve a shared cause's system alert, counting what became of its alerts."""
        firing = recovering = no_data = 0
        with self._sessions() as db:
            event = SelfMonitor.open_event(db, kind)
            if event is None:
                return
            since = event.started_at
            for rule_id, fp, event_id in affected:
                state = db.scalar(select(AlertEvent.state).where(AlertEvent.id == event_id))
                memory = self._open.get((rule_id, fp))
                if state == AlertState.FIRING:
                    firing += 1
                elif state == AlertState.RESOLVED or (memory is not None and memory.recovering):
                    recovering += 1
                elif state == AlertState.NO_DATA:
                    no_data += 1
        took = minutes((now - since).total_seconds())
        what = (
            "Prometheus back"
            if kind == SystemKind.PROMETHEUS_UNREACHABLE
            else "Metrics collected again"
        )
        counts = []
        if firing:
            counts.append(f"{plural(firing, 'alert')} still firing")
        if recovering:
            counts.append(f"{recovering} recovering")
        if no_data:
            counts.append(f"{no_data} still without data")
        tail = f" {', '.join(counts)}." if counts else ""
        self.monitor.resolve_alert(kind, f"{what} after {took}.{tail}")

    def _polled(self, labels: dict[str, str]) -> bool:
        """The series' device is polled by the collector (its metrics reach Prometheus
        only through Vantage's /metrics)."""
        instance_id = labels.get(INSTANCE_ID_LABEL)
        return bool(instance_id) and self._state_of(instance_id or "") is not None

    def _in_failure_streak(self, labels: dict[str, str]) -> bool:
        """The device has failed poll cycles in a row but isn't down yet."""
        instance_id = labels.get(INSTANCE_ID_LABEL)
        if not instance_id:
            return False
        try:
            failed = self._failed_cycles(uuid.UUID(instance_id))
        except ValueError:
            return False
        return failed >= 1 and self._state_of(instance_id) != "down"

    async def _query(self, query: str, max_series: int | None = None) -> QueryResult:
        if self._slots is None:
            self._slots = asyncio.Semaphore(MAX_CONCURRENT_QUERIES)
        async with self._slots:
            request = QueryRequest(query=query)
            try:
                if max_series is None:
                    result = await self._prometheus().query(request)
                else:
                    result = await self._prometheus().query(request, max_series=max_series)
            except QueryError:  # answered or failed, not cancelled: the tick is moving
                self.monitor.progress(self._clock())
                raise
            self.monitor.progress(self._clock())
            return result

    async def _probe(self) -> str | None:
        """None when Prometheus answers; otherwise why not (`UnreachableReason`). An error
        without a transport cause (Prometheus answered, but not as a working API) is a
        server error: ``vector(1)`` is never refused for its own sake."""
        try:
            await self._prometheus().query(QueryRequest(query=PROBE_QUERY))
        except QueryError as exc:
            self.monitor.progress(self._clock())
            return exc.unreachable or "server_error"
        self.monitor.progress(self._clock())
        return None

    async def _fleet(self) -> _Fleet | None:
        """Every device's last observation; None when either query fails (sources are then
        "unknown" for this tick: nothing counts as clean, nothing goes stale)."""
        try:
            polled = await self._query(POLLED_QUERY, INTERNAL_MAX_SERIES)
            scraped = await self._query(SCRAPED_QUERY, INTERNAL_MAX_SERIES)
        except QueryError:
            return None
        fleet = _Fleet()
        for s in polled.series:
            instance_id = s.labels.get(INSTANCE_ID_LABEL)
            ts = _value(s)
            if not instance_id or ts is None or not math.isfinite(ts):
                continue
            interval = self._poll_interval(s.labels.get("vantage_integration", ""))
            window = freshness_window(interval or DEFAULT_POLL_INTERVAL_SECONDS)
            if instance_id not in fleet.polled or fleet.polled[instance_id][0] < ts:
                fleet.polled[instance_id] = (ts, window)
        up_now: dict[str, bool] = {}
        for s in scraped.series:
            instance_id = s.labels.get(INSTANCE_ID_LABEL)
            ts = _value(s)
            if not instance_id or ts is None or not math.isfinite(ts):
                continue
            up_now.setdefault(instance_id, False)
            if ts > 0:
                up_now[instance_id] = True
                self._last_up[instance_id] = max(self._last_up.get(instance_id, 0.0), ts)
        # Forget targets that are gone (out of service discovery, deleted).
        self._last_up = {k: v for k, v in self._last_up.items() if k in up_now}
        for instance_id, up in up_now.items():
            fleet.scraped[instance_id] = (self._last_up.get(instance_id), up)
        return fleet

    def _source(self, labels: dict[str, str], fleet: _Fleet | None, now: datetime) -> sm.Source:
        instance_id = labels.get(INSTANCE_ID_LABEL)
        if not instance_id:
            return sm.NO_SOURCE
        if fleet is None:
            return sm.Source("unknown")
        now_ts = now.timestamp()
        polled = fleet.polled.get(instance_id)
        if polled is not None:
            ts, window = polled
            if now_ts - ts < window:
                return sm.Source("fresh", ts)
            return sm.Source("down" if self._state_of(instance_id) == "down" else "stale", ts)
        scraped = fleet.scraped.get(instance_id)
        if scraped is not None:
            last, up = scraped
            if last is not None and now_ts - last < freshness_window(SCRAPE_INTERVAL_SECONDS):
                return sm.Source("fresh", last)
            return sm.Source("stale" if up else "down", last)
        # No observation at all: not polled yet (e.g. after a restart), dropped from
        # service discovery, or gone.
        return sm.Source("down" if self._state_of(instance_id) == "down" else "stale")

    def _state_of(self, instance_id: str) -> str | None:
        try:
            return self._instance_state(uuid.UUID(instance_id))
        except ValueError:
            return None

    def _set_pending(self, key: tuple[int, str], pending: sm.Pending | None) -> None:
        if pending is None:
            self._pending.pop(key, None)
        else:
            self._pending[key] = pending

    def _check_revision(self, rule: AlertRule) -> None:
        seen = self._seen.get(rule.id)
        expr = normalize_expr(rule.expr)
        if seen is not None and seen.revision != rule.revision:
            # Any edit: RECOVERING counts and witness state start over.
            self._open = {k: v for k, v in self._open.items() if k[0] != rule.id}
            if seen.expr != expr:
                self._pending = {k: v for k, v in self._pending.items() if k[0] != rule.id}
        self._seen[rule.id] = _Seen(rule.revision, expr)

    def _close_orphans(self, event_ids: list[int]) -> None:
        with self._sessions() as db:
            events = list(db.scalars(select(AlertEvent).where(AlertEvent.id.in_(event_ids))))
            closed: list[AlertEvent] = []
            for reason, group in self._orphan_reasons(db, events).items():
                closed += close_events(db, group, reason, self._clock())
            db.commit()
            messages = [event_message(e) for e in closed]
        for message in messages:
            self._hub.publish(message)

    @staticmethod
    def _orphan_reasons(db: Session, events: list[AlertEvent]) -> dict[EndReason, list[AlertEvent]]:
        """Disabled rule → rule_disabled; soft-deleted rule, or ``rule_id`` NULL (the rule
        row went with its instance, ON DELETE CASCADE) → rule_deleted. Only a live push:
        a gone rule has no channels left to message."""
        out: dict[EndReason, list[AlertEvent]] = {}
        for event in events:
            rule = db.get(AlertRule, event.rule_id) if event.rule_id is not None else None
            reason = (
                EndReason.RULE_DISABLED
                if rule is not None and rule.deleted_at is None
                else EndReason.RULE_DELETED
            )
            out.setdefault(reason, []).append(event)
        return out

    # --- one rule --------------------------------------------------------------------------

    async def _witness(
        self, rule: AlertRule, split: promql.Split
    ) -> tuple[dict[str, tuple[str, dict[str, str]]], str | None]:
        """fingerprint -> ("true" | "false", labels) from the rule's value queries, and the
        error if one failed. A series is false only if every side that has it is false."""
        verdicts: dict[str, tuple[str, dict[str, str]]] = {}
        for part in split.parts:
            try:
                result = await self._query(
                    scoped_query(part.value_query, rule.instance_id), INTERNAL_MAX_SERIES
                )
            except QueryError as exc:
                return {}, exc.message
            for s in result.series:
                if not in_scope(rule.instance_id, s.labels):
                    continue
                value = _value(s)
                if value is None:
                    continue
                fp = alert_labels.fingerprint(s.labels)
                verdict = "true" if part.condition.holds(value) else "false"
                if verdicts.get(fp, ("false",))[0] != "true":
                    verdicts[fp] = (verdict, s.labels)
        return verdicts, None

    async def _evaluate(
        self,
        rule: AlertRule,
        open_rows: dict[str, _OpenRow],
        fleet: _Fleet | None,
        metrics_down: bool = False,
        metrics_carriers: set[int] | None = None,
    ) -> None:
        split = promql.split(rule.expr)
        now = self._clock()
        query_ok = True
        error: str | None = None
        current: dict[str, Series] = {}
        try:
            result = await self._query(rule.expr)
        except QueryError as exc:
            query_ok = False
            error = exc.message
        else:
            for s in result.series:
                if in_scope(rule.instance_id, s.labels):
                    current.setdefault(alert_labels.fingerprint(s.labels), s)
        verdicts: dict[str, tuple[str, dict[str, str]]] = {}
        witness_failed = False
        if query_ok and split.mode == "value":
            verdicts, error = await self._witness(rule, split)
            witness_failed = error is not None
        # A config problem, not an outage: shown from the third failure in a row, and
        # never sent to a channel.
        # Logged once when the error starts to show and once when it clears (M1), with the
        # proxy's short, fixed text (never Prometheus' own error).
        if error is None:
            if self._fails.pop(rule.id, 0) >= sm.QUERY_FAILED_TICKS:
                log.info("alert rule %d is evaluated again", rule.id)
            self.set_rule_error(rule.id, None)
        else:
            self._fails[rule.id] = self._fails.get(rule.id, 0) + 1
            if self._fails[rule.id] >= sm.QUERY_FAILED_TICKS:
                if self._fails[rule.id] == sm.QUERY_FAILED_TICKS:
                    log.warning("alert rule %d can't be evaluated (%s)", rule.id, error)
                self.set_rule_error(rule.id, error)

        def observe(fp: str, labels: dict[str, str]) -> sm.Observation:
            shared = None
            wait: timedelta | None = sm.NO_DATA_MESSAGE_AFTER
            if metrics_down and self._polled(labels):
                shared = sm.NoDataReason.METRICS_NOT_COLLECTED
                wait = self._shared_wait(rule, metrics_carriers or set())
            elif self._in_failure_streak(labels):
                wait = None
            witness: sm.Witness = "none"
            if split.mode == "value":
                if witness_failed:
                    witness = "failed"
                elif fp in verdicts:
                    witness = "true" if verdicts[fp][0] == "true" else "false"
                else:
                    witness = "absent"
            return sm.Observation(
                now=now,
                query_ok=query_ok,
                in_result=fp in current,
                witness=witness,
                source=self._source(labels, fleet, now),
                mode=split.mode,
                started_at=self._started_at,
                shared_cause=shared,
                no_data_wait=wait,
            )

        # Open alerts.
        for fp, row in open_rows.items():
            if fp == LEGACY_FINGERPRINT:
                continue
            labels = current[fp].labels if fp in current else row.labels
            await self._step_open(rule, row, observe(fp, labels))
        legacy = open_rows.get(LEGACY_FINGERPRINT)
        if legacy is not None and query_ok:
            free = [fp for fp in current if fp not in open_rows]
            if len(free) == 1:  # unambiguous: the old event belongs to this series
                self._adopt_legacy(legacy.id, free[0], current[free[0]])
                open_rows = {**open_rows, free[0]: legacy}
            elif len(free) > 1:  # can't tell which device it was: close quietly
                self._retire_legacy(legacy.id)

        # Pending timers keep running while an event is open, so a series whose event an
        # admin or a rule edit closes is counted from the close (see _open_event).
        keys = {k[1] for k in self._pending if k[0] == rule.id} | set(current)
        for fp in sorted(keys):
            key = (rule.id, fp)
            labels = current[fp].labels if fp in current else {}
            pending, fire = sm.step_pending(
                self._pending.get(key), observe(fp, labels), rule.for_seconds
            )
            self._set_pending(key, pending)
            if fire and pending is not None and fp in current and fp not in open_rows:
                await self._open_event(rule, fp, current[fp], pending, now, fleet)

    async def _step_open(self, rule: AlertRule, row: _OpenRow, obs: sm.Observation) -> None:
        key = (rule.id, row.fingerprint)
        stored_state: str = "no_data" if row.state == AlertState.NO_DATA else "firing"
        memory = self._open.get(key)
        if memory is None:  # first sight since a restart or a rule edit
            memory = sm.OpenAlert(
                state="firing",
                state_since=row.state_since,
                last_value_at=obs.now,
                no_data_sent=self._no_data_sent(row.id, row.state_since),
            )
        before = replace(
            memory,
            state="no_data" if stored_state == "no_data" else "firing",
            state_since=row.state_since,
            no_data_reason=_reason(row.no_data_reason),
            reminder_sent_at=row.reminder_sent_at,
        )
        after, notices = sm.step_open(before, obs)
        if after is None:
            self._open.pop(key, None)
            await self._resolve(row.id, rule.channel_ids)
            return
        changed = (
            after.state != before.state
            or after.state_since != before.state_since
            or after.no_data_reason != before.no_data_reason
            or after.reminder_sent_at != before.reminder_sent_at
        )
        if changed or notices:
            if not self._store(rule, row, after, notices, obs.now):
                self._open.pop(key, None)  # closed meanwhile (an admin, a rule edit)
                return
        self._open[key] = after

    def _no_data_sent(self, event_id: int, since: datetime, db: Session | None = None) -> bool:
        """Whether the NO DATA message of this event's current NO DATA episode (since
        ``since``) was already queued (sent or folded), after a restart."""
        if db is None:
            with self._sessions() as own:
                return self._no_data_sent(event_id, since, own)
        return (
            db.scalar(
                select(AlertNotification.id)
                .where(
                    AlertNotification.event_id == event_id,
                    AlertNotification.kind == NotificationKind.NO_DATA,
                    AlertNotification.created_at >= since,
                )
                .limit(1)
            )
            is not None
        )

    def _store(
        self,
        rule: AlertRule,
        row: _OpenRow,
        after: sm.OpenAlert,
        notices: list[sm.Notice],
        now: datetime,
    ) -> bool:
        """Write the new state and queue its messages in one transaction. False if the
        event is no longer open as read."""
        new_state = AlertState.NO_DATA if after.state == "no_data" else AlertState.FIRING
        with self._sessions() as db:
            result = db.execute(
                update(AlertEvent)
                .where(AlertEvent.id == row.id, AlertEvent.state == row.state)
                .values(
                    state=new_state,
                    state_since=after.state_since,
                    no_data_reason=after.no_data_reason.value if after.no_data_reason else None,
                    reminder_sent_at=after.reminder_sent_at,
                )
                .execution_options(synchronize_session=False)
            )
            if not result.rowcount:  # type: ignore[attr-defined]
                db.rollback()
                return False
            event = db.get(AlertEvent, row.id)
            if event is None:  # pragma: no cover - just updated in this transaction
                db.rollback()
                return False
            for notice in notices:
                text = self._notice_text(notice, event, after)
                if notice == sm.Notice.NO_DATA:
                    self._queue_no_data(db, rule, event, after.no_data_reason, text, now)
                    continue
                enqueue(
                    db,
                    channel_ids=list(rule.channel_ids),
                    kind=(
                        NotificationKind.FIRING
                        if notice == sm.Notice.DATA_BACK
                        else NotificationKind.NO_DATA
                    ),
                    text=text,
                    event_id=event.id,
                    now=now,
                )
            db.commit()
            message = event_message(event)
        if new_state != row.state or after.no_data_reason != _reason(row.no_data_reason):
            self._hub.publish(message)
        return True

    # --- folding NO DATA messages (P0-4) -------------------------------------------------

    def _queue_no_data(
        self,
        db: Session,
        rule: AlertRule,
        event: AlertEvent,
        reason: sm.NoDataReason | None,
        text: str,
        now: datetime,
    ) -> None:
        """Queue an alert's NO DATA message (no commit): folded into the system alert of a
        shared cause, or into its device's instance-down message, or sent on its own."""
        rows = enqueue(
            db,
            channel_ids=list(rule.channel_ids),
            kind=NotificationKind.NO_DATA,
            text=text,
            event_id=event.id,
            now=now,
        )
        if reason in SHARED_KIND:
            # Only into the system alert's message on the same channel: a channel that
            # doesn't take system alerts gets the alert's own NO DATA message.
            carriers = SelfMonitor.carriers(db, SHARED_KIND[reason])
            for row in rows:
                carrier = carriers.get(row.channel_id or 0)
                if carrier is not None and row.status == NotificationStatus.PENDING:
                    fold_into(db, row, carrier, now)
            return
        if reason is not None and reason.value in DEVICE_REASONS:
            down = self._down_event(db, event)
            if down is not None:
                self._fold_into_down(db, down, rows, now)

    @staticmethod
    def _down_event(db: Session, event: AlertEvent) -> tuple[AlertEvent, list[int]] | None:
        """The open FIRING instance-down event of ``event``'s device (another rule), with
        that rule's channels."""
        if event.instance_id is None:
            return None
        candidates = db.execute(
            select(AlertEvent, AlertRule)
            .join(AlertRule, AlertRule.id == AlertEvent.rule_id)
            .where(
                AlertEvent.instance_id == event.instance_id,
                AlertEvent.state == AlertState.FIRING,
                AlertEvent.source == EventSource.RULE,
                AlertEvent.id != event.id,
            )
            .order_by(AlertEvent.id)
        )
        for down, rule in candidates:
            if promql.is_instance_down(rule.expr):
                return down, list(rule.channel_ids)
        return None

    @staticmethod
    def _fold_into_down(
        db: Session,
        down: tuple[AlertEvent, list[int]],
        rows: list[AlertNotification],
        now: datetime,
    ) -> None:
        """Fold NO DATA rows into the instance-down message on the same channel: its FIRING
        message while that is still queued, otherwise a short follow-up (one per channel,
        reused while queued). Rows on channels the down rule doesn't use stay as they are."""
        event, channels = down
        for row in rows:
            if row.channel_id not in channels or row.status != NotificationStatus.PENDING:
                continue
            while True:
                carrier = db.scalar(
                    select(AlertNotification)
                    .where(
                        AlertNotification.event_id == event.id,
                        AlertNotification.channel_id == row.channel_id,
                        AlertNotification.kind.in_(
                            (NotificationKind.FIRING, NotificationKind.NO_DATA)
                        ),
                        AlertNotification.status == NotificationStatus.PENDING,
                    )
                    .order_by(AlertNotification.id.desc())
                    .limit(1)
                )
                if carrier is None:
                    carrier = AlertNotification(
                        event_id=event.id,
                        channel_id=row.channel_id,
                        kind=NotificationKind.NO_DATA,
                        text="",
                        status=NotificationStatus.PENDING,
                        next_attempt_at=now,
                        created_at=now,
                    )
                    db.add(carrier)
                    db.flush()
                fold_into(db, row, carrier, now)
                n = int(
                    db.scalar(
                        select(func.count()).where(
                            AlertNotification.folded_into == carrier.id,
                            AlertNotification.kind == NotificationKind.NO_DATA,
                        )
                    )
                    or 0
                )
                head = "FIRING" if carrier.kind == NotificationKind.FIRING else "STILL DOWN"
                others = "other alert" if n == 1 else "other alerts"
                text = f"{head}: {event.summary}. {n} {others} on this device can't be checked"
                written = db.execute(
                    update(AlertNotification)
                    .where(
                        AlertNotification.id == carrier.id,
                        AlertNotification.status == NotificationStatus.PENDING,
                    )
                    .values(text=text)
                    .execution_options(synchronize_session=False)
                )
                if written.rowcount:  # type: ignore[attr-defined]
                    break
                # Claimed meanwhile, so its text won't mention this row: take another.
                row.status = NotificationStatus.PENDING
                row.folded_into = None
                db.flush()

    def _fold_waiting(
        self, db: Session, rule: AlertRule, event: AlertEvent, now: datetime
    ) -> list[tuple[int, str]]:
        """An instance-down event just opened: fold the device's other alerts already in
        NO DATA (their message not queued yet) into its message. Returns their keys."""
        if event.instance_id is None or not promql.is_instance_down(rule.expr):
            return []
        waiting = db.execute(
            select(AlertEvent, AlertRule)
            .join(AlertRule, AlertRule.id == AlertEvent.rule_id)
            .where(
                AlertEvent.instance_id == event.instance_id,
                AlertEvent.state == AlertState.NO_DATA,
                AlertEvent.source == EventSource.RULE,
                AlertEvent.no_data_reason.in_(DEVICE_REASONS),
                AlertEvent.id != event.id,
            )
            .order_by(AlertEvent.id)
        ).all()
        folded: list[tuple[int, str]] = []
        for other, other_rule in waiting:
            key = (other_rule.id, other.fingerprint)
            memory = self._open.get(key)
            since = other.state_since or other.started_at
            if (memory is not None and memory.no_data_sent) or self._no_data_sent(
                other.id, since, db
            ):
                continue
            reason = _reason(other.no_data_reason)
            words = NO_DATA_WORDS.get(reason, "no data") if reason else "no data"
            rows = enqueue(
                db,
                channel_ids=list(other_rule.channel_ids),
                kind=NotificationKind.NO_DATA,
                text=f"{NO_DATA_PREFIX}{other.summary} ({words})",
                event_id=other.id,
                now=now,
            )
            self._fold_into_down(db, (event, list(rule.channel_ids)), rows, now)
            folded.append(key)
        return folded

    @staticmethod
    def _notice_text(notice: sm.Notice, event: AlertEvent, alert: sm.OpenAlert) -> str:
        words = NO_DATA_WORDS.get(alert.no_data_reason, "no data") if alert.no_data_reason else ""
        if notice == sm.Notice.NO_DATA:
            return f"{NO_DATA_PREFIX}{event.summary} ({words})"
        if notice == sm.Notice.REMINDER:
            return f"STILL NO DATA after 24 h: {event.summary} ({words})"
        return f"DATA BACK, still firing: {event.summary}"

    def _adopt_legacy(self, event_id: int, fp: str, series: Series) -> None:
        """Give a pre-0006 open event the identity of a series of its rule (no message:
        it was announced when it fired)."""
        with self._sessions() as db:
            event = db.get(AlertEvent, event_id)
            if event is None or event.state not in OPEN_STATES:
                return
            stored = alert_labels.stored_labels(series.labels)
            event.fingerprint = fp
            event.labels = stored
            event.instance_id = label_instance_id(db, stored)
            try:
                db.commit()
            except IntegrityError:
                db.rollback()

    def _retire_legacy(self, event_id: int) -> None:
        """Resolve a pre-0006 open event that matches no single series (no message)."""
        with self._sessions() as db:
            event = db.get(AlertEvent, event_id)
            if event is not None and close_events(
                db, [event], EndReason.RULE_CHANGED, self._clock()
            ):
                db.commit()
                message = event_message(event)
            else:
                return
        self._hub.publish(message)

    async def _open_event(
        self,
        rule: AlertRule,
        fp: str,
        series: Series,
        pending: sm.Pending,
        now: datetime,
        fleet: _Fleet | None,
    ) -> None:
        with self._sessions() as db:
            current = db.get(AlertRule, rule.id)
            if (
                current is None
                or current.revision != rule.revision
                or not current.enabled
                or current.deleted_at is not None
            ):
                return  # edited while the query ran: the next tick re-evaluates it
            # Closed by hand or by a rule edit after it started pending: count again from
            # the close. Covers edits that land between two ticks (disable then enable,
            # scope X then back), which leave the in-memory timer in place.
            closed_at = db.scalar(
                select(func.max(AlertEvent.ended_at)).where(
                    AlertEvent.rule_id == rule.id,
                    AlertEvent.fingerprint == fp,
                    AlertEvent.end_reason.in_(_RECOUNT_AFTER),
                )
            )
            if closed_at is not None and closed_at > pending.started_at:
                since_close = max(0.0, (now - closed_at).total_seconds())
                self._pending[(rule.id, fp)] = replace(
                    pending, started_at=closed_at, elapsed=min(pending.elapsed, since_close)
                )
                if since_close < rule.for_seconds:
                    return
            stored = alert_labels.stored_labels(series.labels)
            event = AlertEvent(
                rule_id=rule.id,
                fingerprint=fp,
                labels=stored,
                instance_id=label_instance_id(db, stored),
                rule_name=rule.name,
                severity=rule.severity,
                state=AlertState.FIRING,
                started_at=now,
                state_since=now,
                summary=summarize(rule.name, rule.severity, stored),
            )
            db.add(event)
            try:
                db.flush()
            except IntegrityError:  # already open (another writer): nothing to announce
                db.rollback()
                return
            enqueue(
                db,
                channel_ids=list(rule.channel_ids),
                kind=NotificationKind.FIRING,
                text=f"FIRING: {event.summary}",
                event_id=event.id,
                now=now,
            )
            folded = self._fold_waiting(db, rule, event, now)
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                return
            message = event_message(event)
        for key in folded:  # their NO DATA message is queued (folded): not again
            if key in self._open:
                self._open[key] = replace(self._open[key], no_data_sent=True)
        source = self._source(series.labels, fleet, now)
        self._open[(rule.id, fp)] = sm.OpenAlert(
            state="firing",
            state_since=now,
            ref_ts=source.ts if source.kind == "fresh" else None,
            last_value_at=now,
        )
        self._hub.publish(message)

    async def _resolve(self, event_id: int, channel_ids: list[int]) -> None:
        with self._sessions() as db:
            event = db.get(AlertEvent, event_id)
            if event is None:
                return
            now = self._clock()
            if not close_events(db, [event], EndReason.RECOVERED, now):
                return
            enqueue(
                db,
                channel_ids=channel_ids,
                kind=NotificationKind.RESOLVED,
                text=f"RESOLVED: {event.summary}",
                event_id=event.id,
                now=now,
            )
            db.commit()
            message = event_message(event)
        self._hub.publish(message)
