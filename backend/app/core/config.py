"""Application settings, read from environment variables.

Only non-secret settings live here. Secrets (master key, Prometheus token) are
loaded by the services that use them (M1.2, M2.3).
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache

from app.core.client_address import IPNetwork, TrustedProxiesError, parse_trusted_proxies

DEFAULT_DATABASE_URL = "sqlite:///./vantage.db"

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}


class ConfigError(ValueError):
    """A setting has an invalid value. The message names the variable, never its value."""


@dataclass(frozen=True)
class Settings:
    database_url: str = DEFAULT_DATABASE_URL
    # Interactive API docs (/docs, /redoc, /openapi.json). Off by default; for development only.
    api_docs: bool = False
    # Mark session cookies Secure. Only turn off to reach the UI over plain HTTP on a LAN
    # before HTTPS is set up (M8.1); browsers treat http://localhost as secure anyway.
    cookie_secure: bool = True
    # A session ends this long after login, or after this long without any request.
    session_max_hours: int = 12
    session_idle_minutes: int = 120
    # Reverse proxies whose X-Real-IP header names the real client (VANTAGE_TRUSTED_PROXIES).
    # Empty = trust nobody: the client address is always the TCP peer.
    trusted_proxies: tuple[IPNetwork, ...] = ()


def _parse_bool(name: str, raw: str) -> bool:
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ConfigError(f"{name} must be true or false")


def _parse_int(name: str, raw: str, default: int, low: int, high: int) -> int:
    value = raw.strip()
    if not value:
        return default
    try:
        number = int(value)
    except ValueError:
        number = low - 1
    if not low <= number <= high:
        raise ConfigError(f"{name} must be a whole number from {low} to {high}")
    return number


def normalize_database_url(url: str) -> str:
    """Pick the driver for plain Postgres URLs, so users can paste a standard URL.

    SQLAlchemy maps ``postgresql://`` to psycopg2, but we ship psycopg 3.
    """
    url = url.strip()
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix) :]
    return url


def _validate_database_url(url: str) -> str:
    if not (url.startswith("sqlite") or url.startswith("postgresql+")):
        # Don't echo the URL: it may contain a password.
        raise ConfigError("VANTAGE_DATABASE_URL must be a sqlite:// or postgresql:// URL")
    return url


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env
    database_url = _validate_database_url(
        normalize_database_url(env.get("VANTAGE_DATABASE_URL") or DEFAULT_DATABASE_URL)
    )
    api_docs = _parse_bool("VANTAGE_API_DOCS", env.get("VANTAGE_API_DOCS", ""))
    cookie_secure = _parse_bool(
        "VANTAGE_COOKIE_SECURE", env.get("VANTAGE_COOKIE_SECURE", "").strip() or "true"
    )
    session_max_hours = _parse_int(
        "VANTAGE_SESSION_MAX_HOURS", env.get("VANTAGE_SESSION_MAX_HOURS", ""), 12, 1, 720
    )
    session_idle_minutes = _parse_int(
        "VANTAGE_SESSION_IDLE_MINUTES", env.get("VANTAGE_SESSION_IDLE_MINUTES", ""), 120, 5, 43200
    )
    try:
        trusted_proxies = parse_trusted_proxies(env.get("VANTAGE_TRUSTED_PROXIES", ""))
    except TrustedProxiesError as exc:
        raise ConfigError(str(exc)) from None
    return Settings(
        database_url=database_url,
        api_docs=api_docs,
        cookie_secure=cookie_secure,
        session_max_hours=session_max_hours,
        session_idle_minutes=session_idle_minutes,
        trusted_proxies=trusted_proxies,
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
