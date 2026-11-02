"""SQLAlchemy models for every table in AGENTS.md Contract 2.

Importing this package registers all tables on ``Base.metadata``.
"""

from app.models.alert import (
    OPEN_STATES,
    AlertEvent,
    AlertRule,
    AlertState,
    EndReason,
    EventSource,
    SystemKind,
)
from app.models.audit import ActorType, AuditLog
from app.models.channel import Channel, ChannelType
from app.models.dashboard import Dashboard
from app.models.instance import Instance
from app.models.link import Link
from app.models.login_failure import LoginFailure
from app.models.notification import AlertNotification, NotificationKind, NotificationStatus
from app.models.session import UserSession
from app.models.setting import Setting
from app.models.user import Role, User

__all__ = [
    "OPEN_STATES",
    "ActorType",
    "AlertEvent",
    "AlertNotification",
    "AlertRule",
    "AlertState",
    "AuditLog",
    "Channel",
    "ChannelType",
    "Dashboard",
    "EndReason",
    "EventSource",
    "Instance",
    "Link",
    "LoginFailure",
    "NotificationKind",
    "NotificationStatus",
    "Role",
    "Setting",
    "SystemKind",
    "User",
    "UserSession",
]
