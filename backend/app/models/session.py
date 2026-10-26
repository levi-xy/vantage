from datetime import datetime

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, utcnow


class UserSession(Base):
    """A login session (M1.3).

    ``id`` is a SHA-256 hash of the token in the user's cookie, never the token itself,
    so a copy of the database can't be used to log in. Deleting the row ends the session.
    """

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(index=True)
    last_seen_at: Mapped[datetime] = mapped_column(default=utcnow)

    def __repr__(self) -> str:  # the id is a hash, but there's no reason to print it
        return f"UserSession(user_id={self.user_id!r}, expires_at={self.expires_at!r})"
