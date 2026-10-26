import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import LargeBinary, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, utcnow


class Instance(Base):
    """A device added by a user, handled by one integration plugin.

    ``config`` holds only non-secret fields. ``SecretStr`` fields of the
    integration's schema go, encrypted, into ``secrets`` (M1.2 / M2.2).
    """

    __tablename__ = "instances"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    type_id: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    site: Mapped[str | None] = mapped_column(String(64), default=None)
    tags: Mapped[list[str]] = mapped_column(default=list)
    enabled: Mapped[bool] = mapped_column(default=True)
    config: Mapped[dict[str, Any]] = mapped_column(default=dict)
    secrets: Mapped[bytes | None] = mapped_column(LargeBinary, default=None)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)

    def __repr__(self) -> str:  # never include config values or secrets
        return f"Instance(id={self.id!r}, type_id={self.type_id!r}, name={self.name!r})"
