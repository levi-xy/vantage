"""The one timezone Vantage shows times in (maintainer-approved 2026-11-07).

Admins choose a single IANA zone (the ``settings`` row ``timezone``; absent = ``UTC``).
Web pages, Telegram messages, log lines and CLI output use it. Stored data stays UTC.
The backend keeps the current zone in memory (loaded at startup, updated on PUT); the
CLI reads the row itself. The clock is the host's: Vantage never sets it.
"""

import zoneinfo
from datetime import UTC, datetime, tzinfo

from sqlalchemy.orm import Session

from app.models import Setting

SETTING_KEY = "timezone"
DEFAULT_ZONE = "UTC"

_current: tzinfo = UTC


def available() -> list[str]:
    return sorted(zoneinfo.available_timezones())


def is_valid(name: object) -> bool:
    return isinstance(name, str) and name in zoneinfo.available_timezones()


def _zone(name: str) -> tzinfo:
    return UTC if name == "UTC" else zoneinfo.ZoneInfo(name)


def stored_name(db: Session) -> str:
    """The configured zone name; ``UTC`` when unset or no longer valid."""
    row = db.get(Setting, SETTING_KEY)
    value = row.value if row is not None else None
    return value if isinstance(value, str) and is_valid(value) else DEFAULT_ZONE


def set_zone(name: str) -> None:
    global _current
    _current = _zone(name if is_valid(name) else DEFAULT_ZONE)


def load(db: Session) -> None:
    set_zone(stored_name(db))


def zone_of(db: Session) -> tzinfo:
    """The stored zone, for code that doesn't share the backend's memory (the CLI)."""
    return _zone(stored_name(db))


def local(at: datetime, zone: tzinfo | None = None) -> datetime:
    """``at`` in the configured zone (a naive value is taken as UTC)."""
    if at.tzinfo is None:
        at = at.replace(tzinfo=UTC)
    return at.astimezone(zone or _current)


def stamp(at: datetime, zone: tzinfo | None = None) -> str:
    """``2026-11-07 14:05:03+01:00``."""
    return local(at, zone).isoformat(sep=" ", timespec="seconds")
