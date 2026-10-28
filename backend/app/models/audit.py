from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, utcnow
from app.models._types import str_enum


class ActorType(StrEnum):
    """Who made an audited change (Contract 2, ``audit_log.actor_type``)."""

    # A request through the API. ``user_id`` is the logged-in user; it is empty for a
    # failed login (nobody is logged in) or once that user is deleted.
    USER = "user"
    # The admin CLI on the server (``python -m app.cli``). ``user_id`` is always empty:
    # the operator is whoever has a shell on the host.
    CLI = "cli"
    # The backend itself, with no request or operator behind it. ``user_id`` is empty.
    SYSTEM = "system"


class AuditLog(Base):
    """One create/update/delete.

    Secret fields in ``changes`` are stored as ``"<changed>"``, never their values (M1.4).
    """

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Kept when the user is deleted, so the history survives.
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None, index=True
    )
    # No default: every write path says who acted (M3.5).
    actor_type: Mapped[ActorType] = mapped_column(str_enum(ActorType, "actor_type"))
    action: Mapped[str] = mapped_column(String(64))
    target_type: Mapped[str] = mapped_column(String(64))
    # String because targets have int or UUID ids.
    target_id: Mapped[str | None] = mapped_column(String(64), default=None)
    at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    changes: Mapped[dict[str, Any]] = mapped_column(default=dict)
