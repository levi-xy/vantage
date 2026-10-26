from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, utcnow


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
    action: Mapped[str] = mapped_column(String(64))
    target_type: Mapped[str] = mapped_column(String(64))
    # String because targets have int or UUID ids.
    target_id: Mapped[str | None] = mapped_column(String(64), default=None)
    at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    changes: Mapped[dict[str, Any]] = mapped_column(default=dict)
