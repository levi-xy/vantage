"""Helpers shared by built-in integrations.

Turning a failed request into a :class:`TestResult` is the same for every HTTP-based
integration, so it lives here instead of being copied into each one. The rule it
enforces: a message shown to the admin never contains an exception's own text, which
can carry URLs (with query strings), headers or response bodies, and so secrets. It
names only the host and port as configured, plus a fixed description of what failed.
"""

import re
import ssl

import httpx

from app.core.net_guard import NetGuardError, parse_host
from app.integrations.base import FirewallHint, TestResult

USER_AGENT = "Vantage"


def checked_host(value: str) -> str:
    """Validate a configured host (IP literal or DNS name) and return its normal form.

    For use in a ``field_validator``: raises ``ValueError``, which pydantic reports as a
    validation error. Uses the SSRF guard's own parser, so a host accepted here is one
    the guard can check later; whether its address is *allowed* is decided at save time
    and again at connect time.
    """
    try:
        parsed = parse_host(value)
    except NetGuardError as exc:
        raise ValueError(str(exc)) from None  # the guard's message holds no input
    return parsed if isinstance(parsed, str) else str(parsed)


def host_port(host: str, port: int) -> str:
    """``host:port`` for a scrape target or a message, with IPv6 literals bracketed."""
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


# How the ssl module words its errors: "[SSL: CERTIFICATE_VERIFY_FAILED] certificate
# verify failed: ...". httpcore re-raises connection errors "from None", so the original
# ssl.SSLError is usually gone from the chain and only its text is left to go on.
_SSL_TEXT = re.compile(r"\[SSL[:\]]|certificate verify failed", re.IGNORECASE)


def _tls_failure(exc: BaseException) -> bool:
    """Whether a connect error came from the TLS handshake. Only classifies: the text
    itself is never shown."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        if isinstance(current, ssl.SSLError) or _SSL_TEXT.search(str(current)):
            return True
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return False


def connection_failure(exc: Exception, *, where: str, hint: FirewallHint) -> TestResult:
    """A secret-free :class:`TestResult` for a request that got no usable response.

    ``where`` is the ``host:port`` as configured. The firewall ``hint`` is attached when
    a firewall is a likely cause: a timeout (packets dropped) or a refused or
    unreachable connection (packets rejected).

    Exceptions this doesn't recognise are re-raised: the instance API catches them and
    reports only their type.
    """
    if isinstance(exc, NetGuardError):
        # Policy, not network: a firewall rule won't help.
        # Contract: NetGuardError messages are safe to display. They hold only the
        # address, the host as configured, or a fixed reason; never a URL, header, body
        # or credential. This line, the instance API and the poll collector all show
        # them verbatim, so any new guard message must keep to that.
        return TestResult(ok=False, message=f"refused by the network guard: {exc}")
    if isinstance(exc, TimeoutError | httpx.TimeoutException):
        return TestResult(ok=False, message=f"timed out waiting for {where}", hint=hint)
    if isinstance(exc, httpx.ConnectError):
        if _tls_failure(exc):
            return TestResult(
                ok=False,
                message=f"TLS handshake with {where} failed (is the certificate trusted?)",
            )
        return TestResult(
            ok=False, message=f"could not connect to {where} (refused or unreachable)", hint=hint
        )
    if isinstance(exc, httpx.HTTPError):
        return TestResult(ok=False, message=f"request to {where} failed ({type(exc).__name__})")
    raise exc
