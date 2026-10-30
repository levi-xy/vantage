"""Alert rules, events and rule templates (Contract 3, M7.1 and M7.3).

Rules: admin only, CSRF checked, every change audited. An expression is checked on save by
running it once as an instant query through the same guarded proxy (and budget) the
browser's queries use: invalid PromQL or a result over budget is refused with 422. If
Prometheus itself is down the rule is saved unchecked; the evaluator keeps trying.
Events: any logged-in user, newest first, paged with ``limit`` / ``before_id`` like ``/audit``.
"""

import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.alerts.templates import TEMPLATES, RuleTemplate
from app.api.deps import AdminDep, AuthDep, DbDep
from app.api.query import PrometheusDep
from app.core import audit
from app.models import AlertEvent, AlertRule, AlertState, Channel, Instance
from app.query.proxy import MAX_QUERY_LENGTH, QueryError, QueryRequest

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
    rule_id: int
    state: AlertState
    started_at: datetime
    ended_at: datetime | None
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


def _snapshot(rule: AlertRule) -> dict[str, object]:
    return _out(rule).model_dump(mode="json", exclude={"id"})


def _get(db: Session, rule_id: int) -> AlertRule:
    rule = db.get(AlertRule, rule_id)
    if rule is None:
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


@router.get("/alerts/rules")
def list_rules(_auth: AdminDep, db: DbDep) -> list[RuleOut]:
    return [_out(r) for r in db.scalars(select(AlertRule).order_by(AlertRule.id))]


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
    rule_id: int, body: RulePatch, auth: AdminDep, db: DbDep, prometheus: PrometheusDep
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
    if changes:
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
    return _out(rule)


@router.delete("/alerts/rules/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_rule(rule_id: int, auth: AdminDep, db: DbDep) -> Response:
    rule = _get(db, rule_id)
    audit.record(
        db,
        actor_type=audit.ActorType.USER,
        action=audit.Action.DELETE,
        target_type="alert_rule",
        target_id=rule.id,
        user_id=auth.user.id,
        changes={"name": rule.name},
    )
    db.delete(rule)  # its events go with it (ON DELETE CASCADE)
    db.commit()
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
    stmt = select(AlertEvent).order_by(AlertEvent.id.desc()).limit(limit)
    if before_id is not None:
        stmt = stmt.where(AlertEvent.id < before_id)
    if state is not None:
        stmt = stmt.where(AlertEvent.state == state)
    return [
        EventOut(
            id=e.id,
            rule_id=e.rule_id,
            state=e.state,
            started_at=e.started_at,
            ended_at=e.ended_at,
            summary=e.summary,
        )
        for e in db.scalars(stmt)
    ]


@router.get("/alerts/templates")
def list_templates(_auth: AdminDep) -> list[RuleTemplate]:
    """Suggested rules. Creating one is a normal ``POST /alerts/rules`` with these fields."""
    return TEMPLATES
