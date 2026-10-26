import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models._types import str_enum


class AlertState(StrEnum):
    FIRING = "firing"
    RESOLVED = "resolved"


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


class AlertEvent(Base):
    __tablename__ = "alert_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    rule_id: Mapped[int] = mapped_column(
        ForeignKey("alert_rules.id", ondelete="CASCADE"), index=True
    )
    state: Mapped[AlertState] = mapped_column(str_enum(AlertState, "alert_state"))
    started_at: Mapped[datetime]
    ended_at: Mapped[datetime | None] = mapped_column(default=None)
    summary: Mapped[str] = mapped_column(Text, default="")
