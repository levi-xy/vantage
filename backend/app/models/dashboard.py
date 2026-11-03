from typing import Any

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class Dashboard(Base):
    __tablename__ = "dashboards"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    # Card positions and per-card settings.
    layout: Mapped[dict[str, Any]] = mapped_column(default=dict)
    # Tab order, dense from 0; the first dashboard by (position, id) is the main one.
    position: Mapped[int] = mapped_column(default=0, server_default="0")
