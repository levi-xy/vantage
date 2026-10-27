"""Bearer-token auth for the endpoints only Prometheus calls (``/sd/targets``, ``/metrics``).

The token is shared only with Prometheus. It is read from ``VANTAGE_PROMETHEUS_TOKEN_FILE``
(preferred, as in ``deploy/docker-compose.yml``) or ``VANTAGE_PROMETHEUS_TOKEN``.

* Neither set: the protected endpoints answer 503 to everyone (fail closed).
* Set but unreadable, empty or shorter than :data:`MIN_TOKEN_LENGTH`: the app refuses
  to start, like it does for a bad master key.

The token is compared in constant time and never appears in a response, log line or
error message.
"""

import hmac
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status

FILE_VAR = "VANTAGE_PROMETHEUS_TOKEN_FILE"
VALUE_VAR = "VANTAGE_PROMETHEUS_TOKEN"
# `openssl rand -hex 32` (the documented way to make one) gives 64 characters.
MIN_TOKEN_LENGTH = 32
_MAX_HEADER_LENGTH = 1024
_HINT = f"at least {MIN_TOKEN_LENGTH} characters (generate with: openssl rand -hex 32)"


class PrometheusTokenError(RuntimeError):
    """The Prometheus token is set but unusable. The message never contains the token."""


class PrometheusToken:
    """Holds the token; never prints it."""

    def __init__(self, value: str) -> None:
        if (
            len(value) < MIN_TOKEN_LENGTH
            or not value.isascii()
            or not value.isprintable()
            or any(c.isspace() for c in value)
        ):
            raise PrometheusTokenError(f"the Prometheus token must be {_HINT}")
        self._value = value.encode("ascii")

    def __repr__(self) -> str:
        return "PrometheusToken(<redacted>)"

    def matches(self, presented: str) -> bool:
        return hmac.compare_digest(presented.encode("utf-8", "replace"), self._value)


def load_prometheus_token(env: Mapping[str, str] | None = None) -> PrometheusToken | None:
    """The configured token, ``None`` if none is configured. Raises on a bad one."""
    env = os.environ if env is None else env
    token_file = env.get(FILE_VAR, "").strip()
    if token_file:
        try:
            raw = Path(token_file).read_text(encoding="ascii")
        except (OSError, UnicodeDecodeError):
            raise PrometheusTokenError(f"{FILE_VAR} is set but the file can't be read") from None
        source = FILE_VAR
    else:
        raw = env.get(VALUE_VAR, "")
        if not raw.strip():
            return None
        source = VALUE_VAR
    try:
        return PrometheusToken(raw.strip())
    except PrometheusTokenError:
        raise PrometheusTokenError(f"{source} must be {_HINT}") from None


def require_prometheus(request: Request) -> None:
    """FastAPI dependency: the request must carry ``Authorization: Bearer <token>``."""
    token: PrometheusToken | None = request.app.state.prometheus_token
    if token is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "no Prometheus token is configured"
        )
    header = request.headers.get("authorization", "")
    scheme, _, presented = header.partition(" ")
    if (
        len(header) > _MAX_HEADER_LENGTH
        or scheme.lower() != "bearer"
        or not token.matches(presented.strip())
    ):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "missing or invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )


PrometheusAuthDep = Annotated[None, Depends(require_prometheus)]
