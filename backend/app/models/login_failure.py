from datetime import datetime

from sqlalchemy import ForeignKey, Index
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class LoginFailure(Base):
    """One failed login for an existing user (M1.8).

    Holds only who and when: never the submitted username, password or client address.
    Rows older than the login window are pruned on every write for that user, and
    refused attempts delete their own row, so a user never has much more than the
    limit's worth of rows (see ``app.core.login_failures``).
    """

    __tablename__ = "login_failures"
    __table_args__ = (Index("ix_login_failures_user_id_at", "user_id", "at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    at: Mapped[datetime]

    def __repr__(self) -> str:
        return f"LoginFailure(user_id={self.user_id!r}, at={self.at!r})"
