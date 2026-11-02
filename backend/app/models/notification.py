from datetime import datetime
from enum import StrEnum

from sqlalchemy import ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models._types import str_enum


class NotificationKind(StrEnum):
    FIRING = "firing"
    RESOLVED = "resolved"
    RULE_CHANGE = "rule_change"
    # Counts the rows a channel's overflow stored as folded.
    SUMMARY = "summary"


class NotificationStatus(StrEnum):
    PENDING = "pending"
    SENDING = "sending"
    DELIVERED = "delivered"
    FAILED = "failed"
    # Carried by another message (a batch or an overflow summary): see ``folded_into``.
    FOLDED = "folded"
    # The channel was deleted before the message was sent.
    CANCELLED = "cancelled"


UNFINISHED = (NotificationStatus.PENDING, NotificationStatus.SENDING)


class AlertNotification(Base):
    """One message for one channel: the delivery outbox (P0-5, maintainer-approved
    2026-11-01). Inserted in the transaction of the alert change it announces; the
    dispatcher (``app.alerts.delivery``) sends it. Rows are never deleted in v0.1.
    """

    __tablename__ = "alert_notifications"
    __table_args__ = (
        Index("ix_alert_notifications_status_next", "status", "next_attempt_at"),
        Index("ix_alert_notifications_channel_id_id", "channel_id", "id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # None for rule-change and summary messages.
    event_id: Mapped[int | None] = mapped_column(
        ForeignKey("alert_events.id", ondelete="SET NULL"), default=None, index=True
    )
    channel_id: Mapped[int | None] = mapped_column(
        ForeignKey("channels.id", ondelete="SET NULL"), default=None
    )
    kind: Mapped[NotificationKind] = mapped_column(
        str_enum(NotificationKind, "alert_notification_kind")
    )
    # Rendered at insert, capped at 4,096 UTF-16 code units; never holds a secret.
    text: Mapped[str] = mapped_column(Text)
    status: Mapped[NotificationStatus] = mapped_column(
        str_enum(NotificationStatus, "alert_notification_status")
    )
    attempts: Mapped[int] = mapped_column(default=0)
    next_attempt_at: Mapped[datetime]
    claimed_at: Mapped[datetime | None] = mapped_column(default=None)
    claim_token: Mapped[str | None] = mapped_column(String(32), default=None)
    folded_into: Mapped[int | None] = mapped_column(
        ForeignKey("alert_notifications.id", ondelete="SET NULL"), default=None
    )
    # A fixed code (``timeout``, ``rate_limited``, ...), never a response text or a token.
    error_kind: Mapped[str | None] = mapped_column(String(32), default=None)
    created_at: Mapped[datetime]
    finished_at: Mapped[datetime | None] = mapped_column(default=None)
