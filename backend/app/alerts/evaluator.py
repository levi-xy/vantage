"""Alert rule evaluator (M7.1; one alert per series since P0-3a).

Every ``interval`` seconds each enabled rule runs as an instant query through the
guarded Prometheus client (``app.query.proxy``, same budget as the browser's queries).
Each series of the result is its own alert, identified by (rule id, fingerprint) (see
``app.alerts.labels``). A series fires once it has been in the result for ``for_seconds``
without a gap, and resolves (``end_reason = recovered``) when a successful result no
longer contains it. A failed query changes nothing. One ``alert_events`` row covers one
episode: created ``firing`` and later set to ``resolved`` with ``ended_at`` and
``end_reason``. A partial unique index keeps one open event per (rule, fingerprint).

Only the "pending since" timestamps live in memory, per (rule, fingerprint). Open events
are re-read from the database on every tick, so a restart neither duplicates nor forgets a
firing alert. An open event from before migration 0006 (fingerprint ``legacy``) is, on the
next successful evaluation: resolved (``recovered``) if the result is empty; adopted by the
series if exactly one series has no event yet; otherwise resolved quietly
(``rule_changed``), and each series opens its own event.

Rule edits close events in the API, in the same transaction as the edit, and bump the
rule's ``revision``. When the evaluator sees a new revision whose expression differs, it
drops that rule's pending timers; it never opens an event for a rule whose revision moved
while its query ran.

Messages (hub and notifier) carry ids, numbers and a short summary built from the event's
stored (filtered, cleaned, capped) labels. Prometheus' error text is never logged or stored.
"""

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.alerts import labels as alert_labels
from app.alerts.labels import INSTANCE_ID_LABEL, INSTANCE_NAME_LABEL, LEGACY_FINGERPRINT, REDACTED
from app.alerts.notifier import Notifier
from app.collectors.hub import EventHub, Message
from app.core.db import utcnow
from app.models import AlertEvent, AlertRule, AlertState, EndReason, Instance
from app.query.proxy import PrometheusClient, QueryError, QueryRequest, Series

log = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECONDS = 30.0
MAX_PART_LENGTH = 48
MAX_SUMMARY_LENGTH = 300
# A series whose event ended for one of these reasons waits the full ``for`` again,
# counted from the close.
_RECOUNT_AFTER = (EndReason.CLOSED_BY_USER, EndReason.RULE_DISABLED, EndReason.RULE_CHANGED)


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
        "started_at": event.started_at.isoformat(),
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
    """Resolve the still-firing ones among ``events`` (no commit). Returns those closed."""
    closed = []
    for event in events:
        result = db.execute(
            update(AlertEvent)
            .where(AlertEvent.id == event.id, AlertEvent.state == AlertState.FIRING)
            .values(state=AlertState.RESOLVED, ended_at=now, end_reason=reason)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount:  # type: ignore[attr-defined]
            db.refresh(event)
            closed.append(event)
    return closed


@dataclass(frozen=True)
class _Seen:
    revision: int
    expr: str


class AlertEvaluator:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        prometheus: Callable[[], PrometheusClient],
        hub: EventHub,
        notifier: Callable[[], Notifier],
        interval: float = DEFAULT_INTERVAL_SECONDS,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._sessions = session_factory
        self._prometheus = prometheus
        self._hub = hub
        self._notifier = notifier
        self._interval = interval
        self._clock = clock
        self._pending_since: dict[tuple[int, str], datetime] = {}
        self._seen: dict[int, _Seen] = {}
        self._task: asyncio.Task[None] | None = None

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

    async def tick(self) -> None:
        with self._sessions() as db:
            rules = list(
                db.scalars(
                    select(AlertRule).where(
                        AlertRule.enabled.is_(True), AlertRule.deleted_at.is_(None)
                    )
                )
            )
            firing = list(
                db.execute(
                    select(AlertEvent.id, AlertEvent.rule_id, AlertEvent.fingerprint).where(
                        AlertEvent.state == AlertState.FIRING
                    )
                )
            )
            db.expunge_all()
        active = {r.id for r in rules}
        open_by_rule: dict[int, dict[str, int]] = {}
        orphans: list[int] = []
        for event_id, rule_id, fp in firing:
            if rule_id in active:
                open_by_rule.setdefault(rule_id, {})[fp] = event_id
            else:
                orphans.append(event_id)
        if orphans:  # safety net: the API closes these when it disables or deletes a rule
            self._close_orphans(orphans)
        self._pending_since = {k: v for k, v in self._pending_since.items() if k[0] in active}
        self._seen = {k: v for k, v in self._seen.items() if k in active}
        for rule in rules:
            self._check_revision(rule)
            try:
                await self._evaluate(rule, open_by_rule.get(rule.id, {}))
            except Exception as exc:
                log.warning("alert rule %d failed (%s)", rule.id, type(exc).__name__)

    def _check_revision(self, rule: AlertRule) -> None:
        seen = self._seen.get(rule.id)
        expr = normalize_expr(rule.expr)
        if seen is not None and seen.revision != rule.revision and seen.expr != expr:
            self._pending_since = {k: v for k, v in self._pending_since.items() if k[0] != rule.id}
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

    async def _evaluate(self, rule: AlertRule, open_events: dict[str, int]) -> None:
        try:
            result = await self._prometheus().query(QueryRequest(query=rule.expr))
        except QueryError:
            return  # Prometheus unavailable or the expr is broken: keep the current state
        current: dict[str, Series] = {}
        for s in result.series:
            if in_scope(rule.instance_id, s.labels):
                current.setdefault(alert_labels.fingerprint(s.labels), s)
        now = self._clock()

        for fp, event_id in open_events.items():
            if fp != LEGACY_FINGERPRINT and fp not in current:
                await self._resolve(event_id, rule.channel_ids)
        legacy_id = open_events.get(LEGACY_FINGERPRINT)
        if legacy_id is not None:
            free = [fp for fp in current if fp not in open_events]
            if not current:  # the condition cleared: an ordinary recovery
                await self._resolve(legacy_id, rule.channel_ids)
            elif len(free) == 1:  # unambiguous: the old event belongs to this series
                self._adopt_legacy(legacy_id, free[0], current[free[0]])
                open_events = {**open_events, free[0]: legacy_id}
            else:  # can't tell which device it was: close quietly, each series opens its own
                self._retire_legacy(legacy_id)

        for key in [k for k in self._pending_since if k[0] == rule.id and k[1] not in current]:
            del self._pending_since[key]
        for fp, series in sorted(current.items()):
            since = self._pending_since.setdefault((rule.id, fp), now)
            if fp in open_events or (now - since).total_seconds() < rule.for_seconds:
                continue
            await self._open(rule, fp, series, since, now)

    def _adopt_legacy(self, event_id: int, fp: str, series: Series) -> None:
        """Give a pre-0006 open event the identity of a series of its rule (no message:
        it was announced when it fired)."""
        with self._sessions() as db:
            event = db.get(AlertEvent, event_id)
            if event is None or event.state != AlertState.FIRING:
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

    async def _open(
        self, rule: AlertRule, fp: str, series: Series, since: datetime, now: datetime
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
            if closed_at is not None and closed_at > since:
                self._pending_since[(rule.id, fp)] = closed_at
                if (now - closed_at).total_seconds() < rule.for_seconds:
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
                summary=summarize(rule.name, rule.severity, stored),
            )
            db.add(event)
            try:
                db.commit()
            except IntegrityError:  # already open (another writer): nothing to announce
                db.rollback()
                return
            message = event_message(event)
            summary = event.summary
        await self._announce(list(rule.channel_ids), message, f"FIRING: {summary}")

    async def _resolve(self, event_id: int, channel_ids: list[int]) -> None:
        with self._sessions() as db:
            event = db.get(AlertEvent, event_id)
            if event is None:
                return
            if not close_events(db, [event], EndReason.RECOVERED, self._clock()):
                return
            db.commit()
            message = event_message(event)
            summary = event.summary
        await self._announce(list(channel_ids), message, f"RESOLVED: {summary}")

    async def _announce(self, channel_ids: list[int], message: Message, text: str) -> None:
        self._hub.publish(message)
        try:
            await self._notifier().send(channel_ids, text)
        except Exception as exc:  # a broken channel must not stop evaluation
            log.warning("alert notification failed (%s)", type(exc).__name__)
