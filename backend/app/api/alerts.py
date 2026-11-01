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
message per rule goes to its channels after the commit, best effort.
"""

import logging
import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.alerts.evaluator import clean, close_events, event_message, normalize_expr
from app.alerts.labels import stored_labels
from app.alerts.templates import TEMPLATES, RuleTemplate
from app.api.deps import AdminDep, AuthDep, DbDep
from app.api.query import PrometheusDep
from app.collectors.hub import EventHub
from app.core import audit
from app.core.db import utcnow
from app.models import AlertEvent, AlertRule, AlertState, Channel, EndReason, Instance
from app.query.proxy import MAX_QUERY_LENGTH, QueryError, QueryRequest

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
    started_at: datetime
    ended_at: datetime | None
    end_reason: EndReason | None
    summary: str


def _out(rule: AlertRule) -> RuleOut:
    return RuleOut(
        id=rule.id,
        name=rule.name,
        expr=rule.expr,
        for_seconds=rule.for_seconds,
        severity=rule.severity,
        instance_id=rule.instance_id,
        channel_ids=list(rule.channel_ids),
        enabled=rule.enabled,
    )


def _event_out(event: AlertEvent, rule_gone: bool) -> EventOut:
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
        started_at=event.started_at,
        ended_at=event.ended_at,
        end_reason=event.end_reason,
        summary=event.summary,
    )


def _snapshot(rule: AlertRule) -> dict[str, object]:
    return _out(rule).model_dump(mode="json", exclude={"id"})


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


async def _check_expr(prometheus: PrometheusDep, expr: str) -> None:
    try:
        await prometheus.query(QueryRequest(query=expr))
    except QueryError as exc:
        if exc.status_code in _REFUSED:
            raise _unprocessable(f"expr refused: {exc.message}") from None


def _firing(db: Session, rule_id: int) -> list[AlertEvent]:
    return list(
        db.scalars(
            select(AlertEvent).where(
                AlertEvent.rule_id == rule_id, AlertEvent.state == AlertState.FIRING
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


async def _after_close(
    request: Request, rule: AlertRule, reason: EndReason, closed: list[AlertEvent]
) -> None:
    """After the commit: live pushes, then one best-effort message for the rule."""
    if not closed:
        return
    hub: EventHub = request.app.state.event_hub
    for event in closed:
        hub.publish(event_message(event))
    n = len(closed)
    text = _CLOSED_TEXT[reason].format(n=n, alerts="alert" if n == 1 else "alerts")
    try:
        await request.app.state.notifier.send(
            list(rule.channel_ids), f"{clean(rule.name, 80)}: {text}"
        )
    except Exception as exc:  # the edit is committed; a broken channel can't undo it
        log.warning("rule change notification failed (%s)", type(exc).__name__)


@router.get("/alerts/rules")
def list_rules(_auth: AdminDep, db: DbDep) -> list[RuleOut]:
    stmt = select(AlertRule).where(AlertRule.deleted_at.is_(None)).order_by(AlertRule.id)
    return [_out(r) for r in db.scalars(stmt)]


@router.get("/alerts/rules/{rule_id}")
def get_rule(rule_id: int, _auth: AdminDep, db: DbDep) -> RuleOut:
    return _out(_get(db, rule_id))


@router.post("/alerts/rules", status_code=status.HTTP_201_CREATED)
async def create_rule(
    body: RuleIn, auth: AdminDep, db: DbDep, prometheus: PrometheusDep
) -> RuleOut:
    _check_refs(db, body.instance_id, body.channel_ids)
    await _check_expr(prometheus, body.expr)
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
    return _out(rule)


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
    if "expr" in sent and sent["expr"] != rule.expr:
        await _check_expr(prometheus, sent["expr"])
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
    if closing is not None:
        await _after_close(request, rule, closing[0], closed)
    return _out(rule)


@router.delete("/alerts/rules/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_rule(rule_id: int, auth: AdminDep, db: DbDep, request: Request) -> Response:
    """Soft delete: the rule disappears from the API and the evaluator, its events stay."""
    rule = _get(db, rule_id)
    now = utcnow()
    rule.deleted_at = now
    rule.revision += 1
    closed = close_events(db, _firing(db, rule.id), EndReason.RULE_DELETED, now)
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
    await _after_close(request, rule, EndReason.RULE_DELETED, closed)
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
    return [
        _event_out(e, rule_id is None or deleted_at is not None)
        for e, deleted_at, rule_id in db.execute(stmt)
    ]


@router.post("/alerts/events/{event_id}/close")
def close_event(event_id: int, auth: AdminDep, db: DbDep, request: Request) -> EventOut:
    """Close an open event by hand (``end_reason = closed_by_user``). If the series is
    still in the rule's result, the evaluator counts ``for`` again from now."""
    event = db.get(AlertEvent, event_id)
    if event is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "alert event not found")
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
    return _event_out(event, rule is None or rule.deleted_at is not None)


@router.get("/alerts/templates")
def list_templates(_auth: AdminDep) -> list[RuleTemplate]:
    """Suggested rules. Creating one is a normal ``POST /alerts/rules`` with these fields."""
    return TEMPLATES
