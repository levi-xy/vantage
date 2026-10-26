"""Application settings, read from environment variables.

Only non-secret settings live here. Secrets (master key, Prometheus token) are
loaded by the services that use them (M1.2, M2.3).
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache

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


def _parse_bool(name: str, raw: str) -> bool:
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ConfigError(f"{name} must be true or false")


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
    return Settings(database_url=database_url, api_docs=api_docs)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
