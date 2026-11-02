"""Alert rules, events and rule templates (Contract 3, M7.1 and M7.3).

Rules: admin only, CSRF checked, every change audited. An expression is checked on save by
running it once as an instant query through the same guarded proxy (and budget) the
browser's queries use: invalid PromQL or a result over budget is refused with 422. If
Prometheus itself is down the rule is saved unchecked; the evaluator keeps trying.
Events: any logged-in user, newest first, paged with ``limit`` / ``before_id`` like ``/audit``.

Rule edits and open events (P0-3a, maintainer-approved 2026-11-01): the edit, the events
it closes and the audit entry commit in one transaction, and ``revision`` goes up. A new
expression (ignoring whitespace) closes every open event (``rule_changed``); a new scope
closes the events outside it (``rule_changed``); disabling closes all (``rule_disabled``);
deleting is a soft delete that closes all (``rule_deleted``). Name, severity, ``for`` and
channels keep them. No rule operation deletes an event row. When events were closed, one
message per rule and channel is queued in the ``alert_notifications`` outbox in that same
transaction (P0-5); live pushes go out after the commit.

Events carry their ``deliveries``: one entry per queued message about the event (P0-5,
maintainer-approved 2026-11-01). ``GET /alerts/delivery-status`` tells any logged-in user
how many messages have gone undelivered for more than 15 minutes (the UI's banner), and
(P0-4) which system alerts are firing: the same banner shows them.

System alerts (P0-4, maintainer-approved 2026-11-02) are events with ``source`` ``system``,
no rule and a ``system_kind``; they close themselves, so they can't be closed by hand.
"""

import logging
import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.alerts.delivery import enqueue, stuck_count
from app.alerts.evaluator import (
    INTERNAL_MAX_SERIES,
    AlertEvaluator,
    clean,
    close_events,
    event_message,
    normalize_expr,
)
from app.alerts.labels import INSTANCE_ID_LABEL, stored_labels
from app.alerts.promql import RecoveryMode, split
from app.alerts.templates import TEMPLATES, RuleTemplate
from app.api.deps import AdminDep, AuthDep, DbDep
from app.api.query import PrometheusDep
from app.collectors.hub import EventHub
from app.core import audit
from app.core.db import utcnow
from app.models import (
    OPEN_STATES,
    AlertEvent,
    AlertNotification,
    AlertRule,
    AlertState,
    Channel,
    EndReason,
    EventSource,
    Instance,
    NotificationKind,
    NotificationStatus,
)
from app.query.proxy import MAX_QUERY_LENGTH, MAX_SERIES, QueryError, QueryRequest, Series

log = logging.getLogger(__name__)

router = APIRouter()

MAX_LIMIT = 200
MAX_FOR_SECONDS = 86_400
MAX_CHANNELS = 20
# Statuses that mean "the query is bad or too big", as opposed to "Prometheus is unavailable".
_REFUSED = (400, 422)


class RuleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Annotated[str, Field(min_length=1, max_length=128)]
    expr: Annotated[str, Field(min_length=1, max_length=MAX_QUERY_LENGTH)]
    for_seconds: Annotated[int, Field(ge=0, le=MAX_FOR_SECONDS)] = 0
    severity: Literal["info", "warning", "critical"] = "warning"
    # Keep only this instance's series (matched on the vantage_instance_id label).
    instance_id: uuid.UUID | None = None
    channel_ids: Annotated[list[int], Field(max_length=MAX_CHANNELS)] = []
    enabled: bool = True


class RulePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Annotated[str, Field(min_length=1, max_length=128)] | None = None
    expr: Annotated[str, Field(min_length=1, max_length=MAX_QUERY_LENGTH)] | None = None
    for_seconds: Annotated[int, Field(ge=0, le=MAX_FOR_SECONDS)] | None = None
    severity: Literal["info", "warning", "critical"] | None = None
    instance_id: uuid.UUID | None = None
    channel_ids: Annotated[list[int], Field(max_length=MAX_CHANNELS)] | None = None
    enabled: bool | None = None


class RuleOut(BaseModel):
    id: int
    name: str
    expr: str
    for_seconds: int
    severity: str
    instance_id: uuid.UUID | None
    channel_ids: list[int]
    enabled: bool
    # P0-3b (maintainer-approved 2026-11-02): how the rule's alerts resolve. "value": the
    # rule's value is seen failing its condition; "empty_result": the result is empty.
    recovery_mode: RecoveryMode = "value"
    # The last query error the evaluator (or the save-time check) saw; short, no secrets.
    error: str | None = None


class DeliveryOut(BaseModel):
    """One message about an event to one channel. Never the text sent or any channel
    setting: ids, statuses, counts and a fixed error code only."""

    # None once the channel is deleted.
    channel_id: int | None
    channel_name: str | None
    kind: NotificationKind
    # "folded" with finished_at null: waiting in an overflow summary that is not sent yet.
    status: NotificationStatus
    attempts: int
    error_kind: str | None
    created_at: datetime
    finished_at: datetime | None


class SystemAlertOut(BaseModel):
    system_kind: str
    # When the problem started.
    since: datetime


class DeliveryStatusOut(BaseModel):
    # Messages unfinished for more than 15 minutes, plus messages that failed within the
    # last 24 hours (counted at once).
    stuck: int
    # Firing system alerts (P0-4), oldest first.
    system_alerts: list[SystemAlertOut] = []


class EventOut(BaseModel):
    id: int
    # None once the rule is deleted.
    rule_id: int | None
    instance_id: uuid.UUID | None
    # Snapshots taken when the event opened.
    rule_name: str
    severity: str
    labels: dict[str, str]
    fingerprint: str
    state: AlertState
    # Why an open event has no data (P0-3b): source_stale, source_down, metric_missing,
    # query_failed, prometheus_unreachable, metrics_not_collected (P0-4); null otherwise.
    no_data_reason: str | None = None
    started_at: datetime
    # When the event entered its current state (null for events from before 0008).
    state_since: datetime | None = None
    ended_at: datetime | None
    end_reason: EndReason | None
    summary: str
    deliveries: list[DeliveryOut] = []
    # P0-4: "rule", or "system" for Vantage's own alerts (no rule; system_kind is one of
    # prometheus_unreachable, metrics_not_collected, evaluator_stalled).
    source: EventSource = EventSource.RULE
    system_kind: str | None = None


def _out(rule: AlertRule, evaluator: AlertEvaluator | None = None) -> RuleOut:
    return RuleOut(
        id=rule.id,
        name=rule.name,
        expr=rule.expr,
        for_seconds=rule.for_seconds,
        severity=rule.severity,
        instance_id=rule.instance_id,
        channel_ids=list(rule.channel_ids),
        enabled=rule.enabled,
        recovery_mode=split(rule.expr).mode,
        error=evaluator.rule_error(rule.id) if evaluator is not None else None,
    )


def _evaluator(request: Request) -> AlertEvaluator:
    evaluator: AlertEvaluator = request.app.state.alert_evaluator
    return evaluator


def _event_out(
    event: AlertEvent, rule_gone: bool, deliveries: list[DeliveryOut] | None = None
) -> EventOut:
    return EventOut(
        id=event.id,
        rule_id=None if rule_gone else event.rule_id,
        instance_id=event.instance_id,
        rule_name=event.rule_name,
        severity=event.severity,
        # Already filtered when stored; filtered again so no raw sensitive value can leave.
        labels=stored_labels({str(k): str(v) for k, v in (event.labels or {}).items()}),
        fingerprint=event.fingerprint,
        state=event.state,
        no_data_reason=event.no_data_reason if event.state == AlertState.NO_DATA else None,
        started_at=event.started_at,
        state_since=event.state_since,
        ended_at=event.ended_at,
        end_reason=event.end_reason,
        summary=event.summary,
        deliveries=deliveries or [],
        source=event.source,
        system_kind=event.system_kind,
    )


def _deliveries(db: Session, event_ids: list[int]) -> dict[int, list[DeliveryOut]]:
    out: dict[int, list[DeliveryOut]] = {}
    if not event_ids:
        return out
    rows = db.execute(
        select(AlertNotification, Channel.name)
        .outerjoin(Channel, Channel.id == AlertNotification.channel_id)
        .where(AlertNotification.event_id.in_(event_ids))
        .order_by(AlertNotification.id)
    )
    for n, channel_name in rows:
        if n.event_id is None:  # pragma: no cover - filtered above
            continue
        out.setdefault(n.event_id, []).append(
            DeliveryOut(
                channel_id=n.channel_id,
                channel_name=channel_name,
                kind=n.kind,
                status=n.status,
                attempts=n.attempts,
                error_kind=n.error_kind,
                created_at=n.created_at,
                finished_at=n.finished_at,
            )
        )
    return out


def _snapshot(rule: AlertRule) -> dict[str, object]:
    return _out(rule).model_dump(mode="json", exclude={"id", "recovery_mode", "error"})


def _get(db: Session, rule_id: int) -> AlertRule:
    rule = db.get(AlertRule, rule_id)
    if rule is None or rule.deleted_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "alert rule not found")
    return rule


def _unprocessable(message: str) -> HTTPException:
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, message)


def _check_refs(db: Session, instance_id: uuid.UUID | None, channel_ids: list[int]) -> None:
    if instance_id is not None and db.get(Instance, instance_id) is None:
        raise _unprocessable("instance not found")
    if channel_ids:
        found = set(db.scalars(select(Channel.id).where(Channel.id.in_(channel_ids))))
        if found != set(channel_ids):
            raise _unprocessable("channel not found")


async def _check_expr(
    prometheus: PrometheusDep, expr: str, instance_id: uuid.UUID | None
) -> str | None:
    """Run the expression (and its witness queries) once. Refuses (422) invalid PromQL, a
    result over budget, and a rule scoped to one instance whose results carry no
    ``vantage_instance_id`` (it could never match). Returns the error to show when
    Prometheus can't answer (the rule is then saved unchecked)."""
    # The expression itself within the browser's budget; its value queries (the witness)
    # with the evaluator's internal limit, which they also run with.
    queries = [(expr, MAX_SERIES)]
    queries += [(p.value_query, INTERNAL_MAX_SERIES) for p in split(expr).parts]
    series: list[Series] = []
    for query, max_series in queries:
        try:
            req = QueryRequest(query=query)
            series += (await prometheus.query(req, max_series=max_series)).series
        except QueryError as exc:
            if exc.status_code in _REFUSED:
                raise _unprocessable(f"expr refused: {exc.message}") from None
            return clean(exc.message, 120)
    if (
        instance_id is not None
        and series
        and not any(INSTANCE_ID_LABEL in s.labels for s in series)
    ):
        raise _unprocessable(
            "expr refused: its results have no vantage_instance_id label, so a rule scoped "
            "to one instance would never match"
        )
    return None


def _firing(db: Session, rule_id: int) -> list[AlertEvent]:
    """The rule's open events (firing or no data)."""
    return list(
        db.scalars(
            select(AlertEvent).where(
                AlertEvent.rule_id == rule_id, AlertEvent.state.in_(OPEN_STATES)
            )
        )
    )


def _to_close(
    db: Session, rule: AlertRule, before: dict[str, object]
) -> tuple[EndReason, list[AlertEvent]] | None:
    """Which open events an edit closes, and why (called after the edit is applied)."""
    if before["enabled"] and not rule.enabled:
        return EndReason.RULE_DISABLED, _firing(db, rule.id)
    if normalize_expr(str(before["expr"])) != normalize_expr(rule.expr):
        return EndReason.RULE_CHANGED, _firing(db, rule.id)
    if before["instance_id"] != (str(rule.instance_id) if rule.instance_id else None):
        if rule.instance_id is None:  # widened to every instance: all still in scope
            return None
        out = [e for e in _firing(db, rule.id) if e.instance_id != rule.instance_id]
        return EndReason.RULE_CHANGED, out
    return None


_CLOSED_TEXT = {
    EndReason.RULE_CHANGED: "Rule changed: {n} open {alerts} closed, re-checking",
    EndReason.RULE_DISABLED: "Rule disabled: {n} open {alerts} closed",
    EndReason.RULE_DELETED: "Rule deleted: {n} open {alerts} closed",
}


def _queue_closed(
    db: Session, rule: AlertRule, reason: EndReason, closed: list[AlertEvent]
) -> None:
    """Before the commit: one message per channel saying how many events the edit closed."""
    if not closed:
        return
    n = len(closed)
    text = _CLOSED_TEXT[reason].format(n=n, alerts="alert" if n == 1 else "alerts")
    enqueue(
        db,
        channel_ids=list(rule.channel_ids),
        kind=NotificationKind.RULE_CHANGE,
        text=f"{clean(rule.name, 80)}: {text}",
    )


def _publish_closed(request: Request, closed: list[AlertEvent]) -> None:
    """After the commit: the live pushes."""
    hub: EventHub = request.app.state.event_hub
    for event in closed:
        hub.publish(event_message(event))


@router.get("/alerts/rules")
def list_rules(_auth: AdminDep, db: DbDep, request: Request) -> list[RuleOut]:
    stmt = select(AlertRule).where(AlertRule.deleted_at.is_(None)).order_by(AlertRule.id)
    evaluator = _evaluator(request)
    return [_out(r, evaluator) for r in db.scalars(stmt)]


@router.get("/alerts/rules/{rule_id}")
def get_rule(rule_id: int, _auth: AdminDep, db: DbDep, request: Request) -> RuleOut:
    return _out(_get(db, rule_id), _evaluator(request))


@router.post("/alerts/rules", status_code=status.HTTP_201_CREATED)
async def create_rule(
    body: RuleIn, auth: AdminDep, db: DbDep, prometheus: PrometheusDep, request: Request
) -> RuleOut:
    _check_refs(db, body.instance_id, body.channel_ids)
    error = await _check_expr(prometheus, body.expr, body.instance_id)
    rule = AlertRule(**body.model_dump())
    db.add(rule)
    db.flush()
    audit.record(
        db,
        actor_type=audit.ActorType.USER,
        action=audit.Action.CREATE,
        target_type="alert_rule",
        target_id=rule.id,
        user_id=auth.user.id,
        changes=_snapshot(rule),
    )
    db.commit()
    evaluator = _evaluator(request)
    evaluator.set_rule_error(rule.id, error)
    return _out(rule, evaluator)


@router.patch("/alerts/rules/{rule_id}")
async def update_rule(
    rule_id: int,
    body: RulePatch,
    auth: AdminDep,
    db: DbDep,
    prometheus: PrometheusDep,
    request: Request,
) -> RuleOut:
    rule = _get(db, rule_id)
    before = _snapshot(rule)
    sent = body.model_dump(exclude_unset=True)
    if any(sent.get(k, 0) is None for k in ("name", "expr", "for_seconds", "severity", "enabled")):
        raise _unprocessable("this field cannot be null")
    if "channel_ids" in sent and sent["channel_ids"] is None:
        raise _unprocessable("this field cannot be null")
    _check_refs(db, sent.get("instance_id"), sent.get("channel_ids") or [])
    error: str | None = None
    new_expr = "expr" in sent and sent["expr"] != rule.expr
    new_scope = sent.get("instance_id") is not None and sent["instance_id"] != rule.instance_id
    checked = new_expr or new_scope
    if checked:
        error = await _check_expr(
            prometheus, sent.get("expr", rule.expr), sent.get("instance_id", rule.instance_id)
        )
    for key, value in sent.items():
        setattr(rule, key, value)
    db.flush()
    changes = audit.diff(before, _snapshot(rule))
    closing = _to_close(db, rule, before) if changes else None
    closed: list[AlertEvent] = []
    if changes:
        rule.revision += 1
        if closing is not None:
            closed = close_events(db, closing[1], closing[0], utcnow())
            _queue_closed(db, rule, closing[0], closed)
        audit.record(
            db,
            actor_type=audit.ActorType.USER,
            action=audit.Action.UPDATE,
            target_type="alert_rule",
            target_id=rule.id,
            user_id=auth.user.id,
            changes=changes,
        )
    db.commit()
    _publish_closed(request, closed)
    evaluator = _evaluator(request)
    if checked:  # the save-time result describes the rule as saved
        evaluator.set_rule_error(rule.id, error)
    return _out(rule, evaluator)


@router.delete("/alerts/rules/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_rule(rule_id: int, auth: AdminDep, db: DbDep, request: Request) -> Response:
    """Soft delete: the rule disappears from the API and the evaluator, its events stay."""
    rule = _get(db, rule_id)
    now = utcnow()
    rule.deleted_at = now
    rule.revision += 1
    closed = close_events(db, _firing(db, rule.id), EndReason.RULE_DELETED, now)
    _queue_closed(db, rule, EndReason.RULE_DELETED, closed)
    audit.record(
        db,
        actor_type=audit.ActorType.USER,
        action=audit.Action.DELETE,
        target_type="alert_rule",
        target_id=rule.id,
        user_id=auth.user.id,
        changes={"name": rule.name},
    )
    db.commit()
    _publish_closed(request, closed)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/alerts/events")
def list_events(
    _auth: AuthDep,
    db: DbDep,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 50,
    before_id: Annotated[int | None, Query(ge=1)] = None,
    state: AlertState | None = None,
) -> list[EventOut]:
    """Newest first. Page backwards by passing the last ``id`` seen as ``before_id``."""
    stmt = (
        select(AlertEvent, AlertRule.deleted_at, AlertRule.id)
        .outerjoin(AlertRule, AlertRule.id == AlertEvent.rule_id)
        .order_by(AlertEvent.id.desc())
        .limit(limit)
    )
    if before_id is not None:
        stmt = stmt.where(AlertEvent.id < before_id)
    if state is not None:
        stmt = stmt.where(AlertEvent.state == state)
    rows = list(db.execute(stmt))
    deliveries = _deliveries(db, [e.id for e, _, _ in rows])
    return [
        _event_out(e, rule_id is None or deleted_at is not None, deliveries.get(e.id))
        for e, deleted_at, rule_id in rows
    ]


@router.get("/alerts/delivery-status")
def delivery_status(_auth: AuthDep, db: DbDep) -> DeliveryStatusOut:
    """For the banner: how many alert messages are stuck (a count only), and the firing
    system alerts."""
    system = db.scalars(
        select(AlertEvent)
        .where(AlertEvent.source == EventSource.SYSTEM, AlertEvent.state.in_(OPEN_STATES))
        .order_by(AlertEvent.id)
    )
    return DeliveryStatusOut(
        stuck=stuck_count(db),
        system_alerts=[
            SystemAlertOut(system_kind=e.system_kind or "", since=e.started_at) for e in system
        ],
    )


@router.post("/alerts/events/{event_id}/close")
def close_event(event_id: int, auth: AdminDep, db: DbDep, request: Request) -> EventOut:
    """Close an open event by hand (``end_reason = closed_by_user``). If the series is
    still in the rule's result, the evaluator counts ``for`` again from now."""
    event = db.get(AlertEvent, event_id)
    if event is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "alert event not found")
    if event.source == EventSource.SYSTEM:
        raise HTTPException(status.HTTP_409_CONFLICT, "a system alert closes by itself")
    if not close_events(db, [event], EndReason.CLOSED_BY_USER, utcnow()):
        raise HTTPException(status.HTTP_409_CONFLICT, "alert event is already closed")
    audit.record(
        db,
        actor_type=audit.ActorType.USER,
        action=audit.Action.UPDATE,
        target_type="alert_event",
        target_id=event.id,
        user_id=auth.user.id,
        changes={"state": "resolved", "end_reason": EndReason.CLOSED_BY_USER.value},
    )
    db.commit()
    hub: EventHub = request.app.state.event_hub
    hub.publish(event_message(event))
    rule = db.get(AlertRule, event.rule_id) if event.rule_id is not None else None
    gone = rule is None or rule.deleted_at is not None
    return _event_out(event, gone, _deliveries(db, [event.id]).get(event.id))


@router.get("/alerts/templates")
def list_templates(_auth: AdminDep) -> list[RuleTemplate]:
    """Suggested rules. Creating one is a normal ``POST /alerts/rules`` with these fields."""
    return TEMPLATES
