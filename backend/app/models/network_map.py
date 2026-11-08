from datetime import datetime
from typing import Any

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, utcnow


class NetworkMap(Base):
    """A user-drawn network map (M9; maintainer-approved 2026-11-07)."""

    __tablename__ = "network_maps"
    # A deleted map's id is never handed out again (a tab open on /map/<id> must not reach
    # a new map).
    __table_args__ = ({"sqlite_autoincrement": True},)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    # Tab order, dense from 0.
    position: Mapped[int] = mapped_column(default=0, server_default="0")
    # The drawing (app.api.map_document.MapDocument), at most 256 KB.
    document: Mapped[dict[str, Any]] = mapped_column(default=dict)
    # Bumped on every drawing save; a save with a stale revision is refused (409).
    revision: Mapped[int] = mapped_column(default=1, server_default="1")
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
