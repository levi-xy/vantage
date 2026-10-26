from datetime import datetime
from enum import StrEnum

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, utcnow
from app.models._types import str_enum


class Role(StrEnum):
    ADMIN = "admin"
    VIEWER = "viewer"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))  # argon2 (M1.3)
    role: Mapped[Role] = mapped_column(str_enum(Role, "role"))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(default=None)

    def __repr__(self) -> str:  # never include password_hash
        return f"User(id={self.id!r}, username={self.username!r}, role={self.role!r})"
