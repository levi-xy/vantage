import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import ForeignKey, Index, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models._types import str_enum


class AlertState(StrEnum):
    FIRING = "firing"
    RESOLVED = "resolved"
    # Open, but no fresh value to tell whether the condition still holds (P0-3b).
    NO_DATA = "no_data"


# States of an open event: at most one per (rule_id, fingerprint).
OPEN_STATES = (AlertState.FIRING, AlertState.NO_DATA)


class EventSource(StrEnum):
    """Who opened an event (P0-4, maintainer-approved 2026-11-02): a rule, or Vantage's
    self-monitoring (``app.alerts.selfcheck``), which needs no rule and no Prometheus."""

    RULE = "rule"
    SYSTEM = "system"


class SystemKind(StrEnum):
    """The fixed kinds of system alert; at most one open event per kind."""

    PROMETHEUS_UNREACHABLE = "prometheus_unreachable"
    METRICS_NOT_COLLECTED = "metrics_not_collected"
    EVALUATOR_STALLED = "evaluator_stalled"


class EndReason(StrEnum):
    """Why an event stopped firing (P0-3a)."""

    RECOVERED = "recovered"
    RULE_DISABLED = "rule_disabled"
    RULE_DELETED = "rule_deleted"
    RULE_CHANGED = "rule_changed"
    CLOSED_BY_USER = "closed_by_user"


class AlertRule(Base):
    __tablename__ = "alert_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    expr: Mapped[str] = mapped_column(Text)  # PromQL
    for_seconds: Mapped[int] = mapped_column(default=0)
    # TODO(maintainer): severity values aren't defined in Contract 2; free text until M7.1.
    severity: Mapped[str] = mapped_column(String(16))
    # A rule scoped to one instance goes away with it, rather than silently becoming global.
    instance_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("instances.id", ondelete="CASCADE"), default=None, index=True
    )
    channel_ids: Mapped[list[int]] = mapped_column(default=list)
    enabled: Mapped[bool] = mapped_column(default=True)
    # Soft delete: a deleted rule is hidden from the API and the evaluator, and its events
    # keep pointing at it (P0-3a, maintainer-approved 2026-11-01).
    deleted_at: Mapped[datetime | None] = mapped_column(default=None)
    # Bumped by every change through the API; the evaluator drops stale in-memory state.
    revision: Mapped[int] = mapped_column(default=0, server_default=text("0"))


class AlertEvent(Base):
    """One firing episode of one series of a rule: (rule_id, fingerprint) while firing.

    ``rule_name``, ``severity``, ``labels`` and ``instance_id`` are snapshots taken when
    the event opens. Events are never deleted by rule operations; ``rule_id`` goes NULL
    only if the rule row itself disappears (an instance-scoped rule whose instance is
    deleted).
    """

    __tablename__ = "alert_events"
    __table_args__ = (
        # At most one open event per alert identity.
        Index(
            "ux_alert_events_open_series",
            "rule_id",
            "fingerprint",
            unique=True,
            sqlite_where=text("state IN ('firing', 'no_data')"),
            postgresql_where=text("state IN ('firing', 'no_data')"),
        ),
        # System alerts have no rule (rule_id NULL, and NULLs never collide in the index
        # above): at most one open event per system_kind (P0-4).
        Index(
            "ux_alert_events_open_system",
            "system_kind",
            unique=True,
            sqlite_where=text("state IN ('firing', 'no_data') AND system_kind IS NOT NULL"),
            postgresql_where=text("state IN ('firing', 'no_data') AND system_kind IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    rule_id: Mapped[int | None] = mapped_column(
        ForeignKey("alert_rules.id", ondelete="SET NULL"), index=True
    )
    fingerprint: Mapped[str] = mapped_column(String(32))
    labels: Mapped[dict[str, Any]] = mapped_column(default=dict)
    instance_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("instances.id", ondelete="SET NULL"), default=None, index=True
    )
    rule_name: Mapped[str] = mapped_column(String(128))
    severity: Mapped[str] = mapped_column(String(16))
    state: Mapped[AlertState] = mapped_column(str_enum(AlertState, "alert_state"))
    started_at: Mapped[datetime]
    ended_at: Mapped[datetime | None] = mapped_column(default=None)
    end_reason: Mapped[EndReason | None] = mapped_column(
        str_enum(EndReason, "alert_end_reason"), default=None
    )
    summary: Mapped[str] = mapped_column(Text, default="")
    # P0-3b: why an open event has no data (``app.alerts.state.NoDataReason``; no CHECK, so
    # new reasons need no migration), since when it is in its current state, and when the
    # one 24-hour NO DATA reminder was queued.
    no_data_reason: Mapped[str | None] = mapped_column(String(32), default=None)
    state_since: Mapped[datetime | None] = mapped_column(default=None)
    reminder_sent_at: Mapped[datetime | None] = mapped_column(default=None)
    # P0-4: ``system`` events have no rule and a fixed ``system_kind`` (``SystemKind``).
    source: Mapped[EventSource] = mapped_column(
        str_enum(EventSource, "alert_event_source"),
        default=EventSource.RULE,
        server_default=EventSource.RULE.value,
    )
    system_kind: Mapped[str | None] = mapped_column(String(32), default=None)
