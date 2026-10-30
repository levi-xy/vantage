"""Alert rule evaluator (M7.1).

Every ``interval`` seconds each enabled rule runs as an instant query through the
guarded Prometheus client (``app.query.proxy``, same budget as the browser's queries).
A rule fires once its result has been non-empty for ``for_seconds`` without a gap, and
resolves when the result is empty again. One ``alert_events`` row covers one episode:
it is created ``firing`` and later set to ``resolved`` with ``ended_at``.

Only the "pending since" timestamps live in memory. Open events are re-read from the
database on every tick, so a restart neither duplicates nor forgets a firing alert (the
``for`` countdown of a not-yet-firing rule simply starts over).

Messages (hub and notifier) carry ids, numbers and a short summary built from label
values that are cleaned and capped. Prometheus' error text is never logged or stored.
"""

import asyncio
import contextlib
import logging
import unicodedata
from collections.abc import Callable
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.alerts.notifier import Notifier
from app.collectors.hub import EventHub, Message
from app.core.db import utcnow
from app.models import AlertEvent, AlertRule, AlertState
from app.query.proxy import PrometheusClient, QueryError, QueryRequest, Series

log = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECONDS = 30.0
MAX_NAMED_SERIES = 3
MAX_PART_LENGTH = 48
MAX_SUMMARY_LENGTH = 300
# Labels the collector adds or Prometheus uses internally: not worth repeating in a summary.
_SKIP_LABELS = frozenset(
    {"__name__", "job", "vantage_instance_id", "vantage_integration", "vantage_site"}
)
_NAME_LABELS = ("vantage_instance", "instance")


def clean(text: str, limit: int = MAX_PART_LENGTH) -> str:
    """Printable text on one line, capped."""
    # Control, format (bidi overrides), surrogate and line-separator characters become spaces.
    text = "".join(" " if unicodedata.category(c)[0] in "CZ" and c != " " else c for c in text)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _describe(labels: dict[str, str]) -> str:
    name = next((labels[k] for k in _NAME_LABELS if labels.get(k)), "")
    extra = [
        f"{k}={v}"
        for k, v in sorted(labels.items())
        if k not in _SKIP_LABELS and k not in _NAME_LABELS and v
    ][:2]
    parts = ([clean(name)] if name else []) + [clean(e) for e in extra]
    return " ".join(parts) or "series"


def summarize(rule: AlertRule, series: list[Series]) -> str:
    shown = "; ".join(_describe(s.labels) for s in series[:MAX_NAMED_SERIES])
    more = len(series) - MAX_NAMED_SERIES
    if more > 0:
        shown += f"; and {more} more"
    return f"{clean(rule.name, 80)} ({rule.severity}): {shown}"[:MAX_SUMMARY_LENGTH]


def event_message(event: AlertEvent, severity: str) -> Message:
    return {
        "type": "alert_event",
        "event_id": event.id,
        "rule_id": event.rule_id,
        "severity": severity,
        "state": event.state.value,
        "started_at": event.started_at.isoformat(),
        "ended_at": event.ended_at.isoformat() if event.ended_at else None,
        "summary": event.summary,
    }


def _in_scope(rule: AlertRule, series: Series) -> bool:
    """A rule tied to one instance only counts that instance's series."""
    if rule.instance_id is None:
        return True
    return series.labels.get("vantage_instance_id") == str(rule.instance_id)


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
        self._pending_since: dict[int, datetime] = {}
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
            rules = list(db.scalars(select(AlertRule).where(AlertRule.enabled.is_(True))))
            enabled = {r.id for r in rules}
            stale = [
                e.id
                for e in db.scalars(select(AlertEvent).where(AlertEvent.state == AlertState.FIRING))
                if e.rule_id not in enabled
            ]
            db.expunge_all()
        for event_id in stale:  # the rule was disabled while firing
            await self._resolve(event_id)
        self._pending_since = {k: v for k, v in self._pending_since.items() if k in enabled}
        for rule in rules:
            try:
                await self._evaluate(rule)
            except Exception as exc:
                log.warning("alert rule %d failed (%s)", rule.id, type(exc).__name__)

    async def _evaluate(self, rule: AlertRule) -> None:
        try:
            result = await self._prometheus().query(QueryRequest(query=rule.expr))
        except QueryError:
            return  # Prometheus unavailable or the expr is broken: keep the current state
        series = [s for s in result.series if _in_scope(rule, s)]
        now = self._clock()
        if not series:
            self._pending_since.pop(rule.id, None)
            event_id = self._open_event_id(rule.id)
            if event_id is not None:
                await self._resolve(event_id)
            return
        since = self._pending_since.setdefault(rule.id, now)
        if (now - since).total_seconds() < rule.for_seconds:
            return
        if self._open_event_id(rule.id) is not None:
            return
        channel_ids = list(rule.channel_ids)
        with self._sessions() as db:
            event = AlertEvent(
                rule_id=rule.id,
                state=AlertState.FIRING,
                started_at=now,
                summary=summarize(rule, series),
            )
            db.add(event)
            db.commit()
            message = event_message(event, rule.severity)
            summary = event.summary
        await self._announce(channel_ids, message, f"FIRING: {summary}")

    def _open_event_id(self, rule_id: int) -> int | None:
        with self._sessions() as db:
            return db.scalar(
                select(AlertEvent.id).where(
                    AlertEvent.rule_id == rule_id, AlertEvent.state == AlertState.FIRING
                )
            )

    async def _resolve(self, event_id: int) -> None:
        with self._sessions() as db:
            event = db.get(AlertEvent, event_id)
            rule = db.get(AlertRule, event.rule_id) if event else None
            if event is None or rule is None or event.state != AlertState.FIRING:
                return
            event.state = AlertState.RESOLVED
            event.ended_at = self._clock()
            db.commit()
            message = event_message(event, rule.severity)
            summary = event.summary
            channel_ids = list(rule.channel_ids)
        await self._announce(channel_ids, message, f"RESOLVED: {summary}")

    async def _announce(self, channel_ids: list[int], message: Message, text: str) -> None:
        self._hub.publish(message)
        try:
            await self._notifier().send(channel_ids, text)
        except Exception as exc:  # a broken channel must not stop evaluation
            log.warning("alert notification failed (%s)", type(exc).__name__)
