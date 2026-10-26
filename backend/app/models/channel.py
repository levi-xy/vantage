from enum import StrEnum
from typing import Any

from sqlalchemy import LargeBinary, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models._types import str_enum


class ChannelType(StrEnum):
    TELEGRAM = "telegram"


class Channel(Base):
    """An alert notification channel. Secrets (e.g. bot token) are encrypted in ``secrets``."""

    __tablename__ = "channels"

    id: Mapped[int] = mapped_column(primary_key=True)
    type: Mapped[ChannelType] = mapped_column(str_enum(ChannelType, "channel_type"))
    name: Mapped[str] = mapped_column(String(128))
    config: Mapped[dict[str, Any]] = mapped_column(default=dict)
    secrets: Mapped[bytes | None] = mapped_column(LargeBinary, default=None)

    def __repr__(self) -> str:  # never include config values or secrets
        return f"Channel(id={self.id!r}, type={self.type!r}, name={self.name!r})"
