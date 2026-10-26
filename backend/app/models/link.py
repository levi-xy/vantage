import uuid

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class Link(Base):
    """A cable or connection between two instances, for the network map."""

    __tablename__ = "links"

    id: Mapped[int] = mapped_column(primary_key=True)
    a_instance_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("instances.id", ondelete="CASCADE"), index=True
    )
    b_instance_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("instances.id", ondelete="CASCADE"), index=True
    )
    label: Mapped[str | None] = mapped_column(String(128), default=None)
    # Port names as the device reports them (e.g. "ether1", "igb0"), so strings, not numbers.
    a_port: Mapped[str | None] = mapped_column(String(64), default=None)
    b_port: Mapped[str | None] = mapped_column(String(64), default=None)
