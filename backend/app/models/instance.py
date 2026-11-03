import uuid
from collections import Counter
from collections.abc import Iterable
from datetime import datetime
from typing import Any, Literal, get_args

from sqlalchemy import LargeBinary, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, utcnow

# Device colours (maintainer-approved 2026-11-03): every card of a device shares one, so
# its cards are found at a glance. Named colours only; the frontend holds the light and
# dark shades (src/app/deviceColors.ts). No red, amber or green: those mean problems or
# healthy. The order is the auto-assignment order, most distinct first.
DeviceColor = Literal[
    "blue",
    "violet",
    "teal",
    "pink",
    "orange",
    "lime",
    "cyan",
    "indigo",
    "fuchsia",
    "sky",
    "purple",
]
DEVICE_COLORS: tuple[str, ...] = get_args(DeviceColor)


def next_color(used: Iterable[str | None]) -> str:
    """The first palette colour no device uses yet; once all are used, the least used one
    (palette order breaks ties), so assignment cycles through the palette."""
    counts = Counter(c for c in used if c in DEVICE_COLORS)
    return min(DEVICE_COLORS, key=lambda c: counts[c])


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
    # One of DEVICE_COLORS (validated by the API); assigned on create when none is given.
    color: Mapped[str | None] = mapped_column(String(16), default=None)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)

    def __repr__(self) -> str:  # never include config values or secrets
        return f"Instance(id={self.id!r}, type_id={self.type_id!r}, name={self.name!r})"
