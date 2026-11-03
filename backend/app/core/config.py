"""Application settings, read from environment variables.

Only non-secret settings live here. Secrets (master key, Prometheus token) are
loaded by the services that use them (M1.2, M2.3).
"""

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from urllib.parse import urlsplit

from app.core.client_address import IPNetwork, TrustedProxiesError, parse_trusted_proxies

DEFAULT_DATABASE_URL = "sqlite:///./vantage.db"
# The bundled Prometheus on the compose network (deploy/docker-compose.yml).
DEFAULT_PROMETHEUS_URL = "http://prometheus:9090"
# Matches compose's default for Prometheus' --storage.tsdb.retention.time.
DEFAULT_PROMETHEUS_RETENTION = "30d"
# An instance goes down after this many failed poll cycles in a row.
DEFAULT_DOWN_AFTER_FAILED_CYCLES = 3
# Level of the backend's own log lines (VANTAGE_LOG_LEVEL).
DEFAULT_LOG_LEVEL = "INFO"
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
# A Prometheus duration, e.g. 30d, 720h or 1y2w.
_DURATION = re.compile(r"(?:[0-9]+(?:ms|s|m|h|d|w|y))+")

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
    # Where the /query proxy reaches Prometheus (VANTAGE_PROMETHEUS_URL). Server config only:
    # no request can change it.
    prometheus_url: str = DEFAULT_PROMETHEUS_URL
    # Host names (optionally host:port) the dashboard is reached by (VANTAGE_PUBLIC_HOSTS).
    # Empty = any Host. When set, /api/* and /ws requests with another Host are refused,
    # which stops DNS-rebinding attacks from a web page the admin visits.
    public_hosts: tuple[str, ...] = ()
    # How far back Prometheus keeps history (VANTAGE_PROMETHEUS_RETENTION, the same variable
    # compose passes to Prometheus). Used once at startup to seed last_success_at.
    prometheus_retention: str = DEFAULT_PROMETHEUS_RETENTION
    # Failed poll cycles in a row before an instance is down (VANTAGE_DOWN_AFTER_FAILED_CYCLES).
    down_after_failed_cycles: int = DEFAULT_DOWN_AFTER_FAILED_CYCLES
    # The backend's own log level (VANTAGE_LOG_LEVEL): DEBUG, INFO, WARNING or ERROR.
    log_level: str = DEFAULT_LOG_LEVEL


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


def _validate_prometheus_url(raw: str) -> str:
    url = raw.strip().rstrip("/") or DEFAULT_PROMETHEUS_URL
    parts = urlsplit(url)
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
    ):
        # Don't echo the URL: a mistyped one may carry credentials.
        raise ConfigError(
            "VANTAGE_PROMETHEUS_URL must be an http:// or https:// URL without credentials"
        )
    return url


def _split_host(value: str) -> tuple[str, str | None]:
    """``host`` or ``host:port`` (IPv6 in brackets) -> (lowercase host, port or None)."""
    value = value.strip().lower()
    if value.startswith("["):
        host, sep, rest = value.partition("]")
        host += sep
        port = rest[1:] if rest.startswith(":") else None
        if rest and port is None:
            return "", None
    else:
        host, sep, port_text = value.rpartition(":")
        if not sep:
            host, port = value, None
        else:
            port = port_text
        if ":" in host:  # an IPv6 address without brackets: ambiguous, refused
            return "", None
    return host, port


def host_allowed(header: str, allowed: tuple[str, ...]) -> bool:
    """Whether a Host header names an allowed host. An entry without a port allows any port."""
    host, port = _split_host(header)
    if not host:
        return False
    for entry in allowed:
        want_host, want_port = _split_host(entry)
        if host == want_host and (want_port is None or want_port == port):
            return True
    return False


def _parse_public_hosts(raw: str) -> tuple[str, ...]:
    entries = tuple(e.strip().lower() for e in raw.split(",") if e.strip())
    for entry in entries:
        host, port = _split_host(entry)
        if (
            not host
            or any(c in host for c in "/@ ")
            or (port is not None and not (port.isdigit() and 0 < int(port) < 65536))
        ):
            raise ConfigError("VANTAGE_PUBLIC_HOSTS must list host or host:port entries")
    return entries


def _validate_retention(raw: str) -> str:
    value = raw.strip() or DEFAULT_PROMETHEUS_RETENTION
    if not _DURATION.fullmatch(value):
        raise ConfigError("VANTAGE_PROMETHEUS_RETENTION must be a Prometheus duration like 30d")
    return value


def _validate_log_level(raw: str) -> str:
    value = raw.strip().upper() or DEFAULT_LOG_LEVEL
    if value not in LOG_LEVELS:
        raise ConfigError("VANTAGE_LOG_LEVEL must be one of " + ", ".join(LOG_LEVELS))
    return value


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
    prometheus_url = _validate_prometheus_url(env.get("VANTAGE_PROMETHEUS_URL", ""))
    public_hosts = _parse_public_hosts(env.get("VANTAGE_PUBLIC_HOSTS", ""))
    prometheus_retention = _validate_retention(env.get("VANTAGE_PROMETHEUS_RETENTION", ""))
    down_after_failed_cycles = _parse_int(
        "VANTAGE_DOWN_AFTER_FAILED_CYCLES",
        env.get("VANTAGE_DOWN_AFTER_FAILED_CYCLES", ""),
        DEFAULT_DOWN_AFTER_FAILED_CYCLES,
        1,
        10,
    )
    log_level = _validate_log_level(env.get("VANTAGE_LOG_LEVEL", ""))
    return Settings(
        database_url=database_url,
        api_docs=api_docs,
        cookie_secure=cookie_secure,
        session_max_hours=session_max_hours,
        session_idle_minutes=session_idle_minutes,
        trusted_proxies=trusted_proxies,
        prometheus_url=prometheus_url,
        public_hosts=public_hosts,
        prometheus_retention=prometheus_retention,
        down_after_failed_cycles=down_after_failed_cycles,
        log_level=log_level,
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
