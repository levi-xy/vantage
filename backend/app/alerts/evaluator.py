"""Alert rule evaluator (M7.1; one alert per series since P0-3a; state machine P0-3b).

Every ``interval`` seconds the evaluator runs, through the guarded Prometheus client
(``app.query.proxy``, same budget as the browser's queries):

1. a probe (``vector(1)``). If it fails, the tick changes no state: open alerts stay as
   they are, pending timers pause, nothing counts;
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
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.alerts import labels as alert_labels
from app.alerts import promql
from app.alerts import state as sm
from app.alerts.delivery import enqueue
from app.alerts.labels import INSTANCE_ID_LABEL, INSTANCE_NAME_LABEL, LEGACY_FINGERPRINT, REDACTED
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
    Instance,
    NotificationKind,
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
}
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
            self._task = asyncio.create_task(self._run(), name="vantage-alert-evaluator")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

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

        now = self._clock()
        if self._started_at is None:
            self._started_at = now
        if not await self._probe():
            # Prometheus is unreachable: no state changes, pending timers pause.
            obs = sm.Observation(now=now, probe_ok=False)
            for key, pending in list(self._pending.items()):
                rule = next((r for r in rules if r.id == key[0]), None)
                new, _ = sm.step_pending(pending, obs, rule.for_seconds if rule else 0)
                self._set_pending(key, new)
            return
        fleet = await self._fleet()
        for rule in rules:
            self._check_revision(rule)

        async def evaluate(rule: AlertRule) -> None:
            try:
                await self._evaluate(rule, open_by_rule.get(rule.id, {}), fleet)
            except Exception as exc:
                log.warning("alert rule %d failed (%s)", rule.id, type(exc).__name__)

        # Rules run side by side; _query keeps at most MAX_CONCURRENT_QUERIES in flight.
        await asyncio.gather(*(evaluate(rule) for rule in rules))

    async def _query(self, query: str, max_series: int | None = None) -> QueryResult:
        if self._slots is None:
            self._slots = asyncio.Semaphore(MAX_CONCURRENT_QUERIES)
        async with self._slots:
            if max_series is None:
                return await self._prometheus().query(QueryRequest(query=query))
            return await self._prometheus().query(QueryRequest(query=query), max_series=max_series)

    async def _probe(self) -> bool:
        try:
            await self._prometheus().query(QueryRequest(query=PROBE_QUERY))
        except QueryError:
            return False
        return True

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
        self, rule: AlertRule, open_rows: dict[str, _OpenRow], fleet: _Fleet | None
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
        self.set_rule_error(rule.id, error)  # once per tick: no flicker

        def observe(fp: str, labels: dict[str, str]) -> sm.Observation:
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

    def _no_data_sent(self, event_id: int, since: datetime) -> bool:
        """Whether the NO DATA message of this event's current NO DATA episode (since
        ``since``) was already queued, after a restart."""
        with self._sessions() as db:
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
                enqueue(
                    db,
                    channel_ids=list(rule.channel_ids),
                    kind=(
                        NotificationKind.FIRING
                        if notice == sm.Notice.DATA_BACK
                        else NotificationKind.NO_DATA
                    ),
                    text=self._notice_text(notice, event, after),
                    event_id=event.id,
                    now=now,
                )
            db.commit()
            message = event_message(event)
        if new_state != row.state or after.no_data_reason != _reason(row.no_data_reason):
            self._hub.publish(message)
        return True

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
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                return
            message = event_message(event)
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
