"""SQLAlchemy models for every table in AGENTS.md Contract 2.

Importing this package registers all tables on ``Base.metadata``.
"""

from app.models.alert import AlertEvent, AlertRule, AlertState
from app.models.audit import AuditLog
from app.models.channel import Channel, ChannelType
from app.models.dashboard import Dashboard
from app.models.instance import Instance
from app.models.link import Link
from app.models.setting import Setting
from app.models.user import Role, User

__all__ = [
    "AlertEvent",
    "AlertRule",
    "AlertState",
    "AuditLog",
    "Channel",
    "ChannelType",
    "Dashboard",
    "Instance",
    "Link",
    "Role",
    "Setting",
    "User",
]
