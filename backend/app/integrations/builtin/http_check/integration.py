"""HTTP check: does a URL answer, with the expected status, and how fast?

Reference integration (M2.5) and the template for **polled** integrations: Vantage
itself makes the request on every poll and exposes the result on its own ``/metrics``.
Read it alongside ``linux_host``, the template for **scraped** integrations.

What it shows:

- a config schema with a URL field, an optional secret, and validation that refuses a
  URL carrying credentials;
- ``poll()`` that awaits all its I/O, uses only ``ctx.http_client()`` (the SSRF-guarded
  client) and returns ``vantage_http_check_*`` samples without the standard labels
  (the collector adds those);
- ``test_connection()`` that reports failures without echoing exception text, with a
  firewall hint when a firewall is a likely cause.

Only ``GET`` is ever sent, and redirects are not followed: the response to the
configured URL is the result. That also means the bearer token is only ever sent to the
configured URL, never to a host a redirect points at.

This check is HTTP only; ICMP ping is the separate ``ping_check`` integration.
"""

import asyncio
import time
from dataclasses import dataclass
from typing import ClassVar

import httpx
from pydantic import BaseModel, Field, SecretStr, field_validator

from app.integrations.base import (
    CardSpec,
    Category,
    FirewallHint,
    InstanceContext,
    Integration,
    Sample,
    TestResult,
)
from app.integrations.builtin.common import (
    USER_AGENT,
    checked_host,
    connection_failure,
    host_port,
)

TYPE_ID = "http_check"
# Metric names: vantage_<type_id>_<metric> (Contract 1).
METRIC_SUCCESS = f"vantage_{TYPE_ID}_success"
METRIC_STATUS = f"vantage_{TYPE_ID}_status_code"
METRIC_DURATION = f"vantage_{TYPE_ID}_duration_seconds"

# The guarded client stops any request after 15 s in total; a longer limit here would
# never take effect.
MAX_TIMEOUT_SECONDS = 15


class HttpCheckConfig(BaseModel):
    """What the admin enters. The JSON schema of this model drives the UI form.

    Naming matters: ``url`` is found by name by the SSRF check on save (fields named
    ``host``, ``*_host``, ``url`` or ``*_url``). Secrets are top-level ``SecretStr``
    fields and nothing else; their descriptions never show an example value.
    """

    url: str = Field(
        title="URL",
        # Query strings stay allowed (health endpoints use them), so the warning is the
        # safeguard: the URL is stored in the plaintext config and returned by GET.
        description=(
            "http:// or https:// address to request. Everything in it, including the "
            "query string, is stored in plaintext and shown to every user: never put a "
            "token, password or API key in the URL. Use the bearer token field for "
            "credentials."
        ),
        max_length=2048,
        examples=["http://10.0.0.10/health"],
    )
    expected_status: int | None = Field(
        default=None,
        ge=100,
        le=599,
        title="Expected status",
        description=(
            "HTTP status that counts as success. Empty: any 2xx or 3xx "
            "(redirects are not followed)."
        ),
    )
    verify_tls: bool = Field(
        default=True,
        title="Verify TLS certificate",
        description="Turn off only for a device with a self-signed certificate.",
    )
    timeout_seconds: int = Field(
        default=10,
        ge=1,
        le=MAX_TIMEOUT_SECONDS,
        title="Timeout (seconds)",
    )
    bearer_token: SecretStr | None = Field(
        default=None,
        title="Bearer token",
        description="Optional. Sent as an Authorization header to this URL only.",
    )

    @field_validator("url")
    @classmethod
    def _check_url(cls, value: str) -> str:
        # Error messages never repeat the value: it could hold a token in its query.
        value = value.strip()
        try:
            url = httpx.URL(value)
        except httpx.InvalidURL:
            raise ValueError("not a valid URL") from None
        if url.scheme not in ("http", "https") or not url.raw_host:
            raise ValueError("must be an http:// or https:// URL with a host")
        if url.userinfo:
            # The config column is plaintext and returned by the API.
            raise ValueError("must not contain a username or password")
        # Host parsing is duplicated in instance_config.configured_hosts (the SSRF check
        # on save); the two must agree on which host a URL names, or a URL could pass one
        # check and fail (or skip) the other. raw_host is the ASCII/punycode form that
        # the guard, the resolver and the connection all use; httpx's url.host decodes
        # IDN labels to Unicode, which parse_host refuses.
        checked_host(url.raw_host.decode("ascii"))
        return value

    def accepts(self, status: int) -> bool:
        if self.expected_status is not None:
            return status == self.expected_status
        return 200 <= status < 400

    def expectation(self) -> str:
        return str(self.expected_status) if self.expected_status is not None else "2xx or 3xx"

    def target(self) -> tuple[str, int]:
        """``(host, port)`` the check connects to, for messages and firewall hints."""
        url = httpx.URL(self.url)
        host = url.raw_host.decode("ascii")
        return host, url.port or (443 if url.scheme == "https" else 80)


@dataclass(frozen=True)
class _Outcome:
    status: int
    seconds: float
    success: bool


async def _check(ctx: InstanceContext, cfg: HttpCheckConfig) -> _Outcome:
    """Send one GET and time it until the response headers arrive.

    Raises on anything that isn't an HTTP response (timeout, refused connection,
    TLS failure, the SSRF guard refusing the address). Callers decide what that means.
    """
    headers = {"User-Agent": USER_AGENT}
    if cfg.bearer_token is not None:
        headers["Authorization"] = f"Bearer {cfg.bearer_token.get_secret_value()}"
    # asyncio.timeout bounds the whole request (DNS, connect, TLS, headers); the guarded
    # client adds its own 15 s cap underneath.
    async with (
        asyncio.timeout(cfg.timeout_seconds),
        ctx.http_client(verify=cfg.verify_tls, headers=headers) as client,
    ):
        started = time.perf_counter()
        # stream=True: the body is never downloaded, so a large page costs nothing.
        async with client.stream("GET", cfg.url) as response:
            seconds = time.perf_counter() - started
            status = response.status_code
    return _Outcome(status=status, seconds=seconds, success=cfg.accepts(status))


class HttpCheck(Integration):
    type_id = TYPE_ID
    display_name = "HTTP check"
    category: ClassVar[Category] = "generic"
    version = "0.1.0"
    config_schema = HttpCheckConfig

    async def test_connection(self, ctx: InstanceContext) -> TestResult:
        cfg = ctx.config_as(HttpCheckConfig)
        host, port = cfg.target()
        try:
            outcome = await _check(ctx, cfg)
        except Exception as exc:
            return connection_failure(exc, where=host_port(host, port), hint=self._hint(host, port))
        if not outcome.success:
            return TestResult(
                ok=False,
                message=f"HTTP {outcome.status}, expected {cfg.expectation()}",
            )
        return TestResult(
            ok=True, message=f"HTTP {outcome.status} in {outcome.seconds * 1000:.0f} ms"
        )

    async def poll(self, ctx: InstanceContext) -> list[Sample]:
        """One check per poll.

        No response at all (timeout, refused, TLS or guard failure) raises: the collector
        then marks the instance ``down`` and sets ``vantage_instance_up`` to 0. A response
        with the wrong status is still a result: ``success`` is 0 and the status code is
        kept, so an alert rule can say *what* the endpoint answered.
        """
        cfg = ctx.config_as(HttpCheckConfig)
        outcome = await _check(ctx, cfg)
        return [
            Sample(name=METRIC_SUCCESS, value=1.0 if outcome.success else 0.0, type="gauge"),
            Sample(name=METRIC_STATUS, value=float(outcome.status), type="gauge"),
            Sample(name=METRIC_DURATION, value=outcome.seconds, type="gauge"),
        ]

    def poll_interval_seconds(self) -> int:
        return 60

    def cards(self) -> list[CardSpec]:
        return [CardSpec(card_type="http-check", title="HTTP check", default_size=(2, 2))]

    def firewall_hints(self, ctx: InstanceContext) -> list[FirewallHint]:
        host, port = ctx.config_as(HttpCheckConfig).target()
        return [self._hint(host, port)]

    @staticmethod
    def _hint(host: str, port: int) -> FirewallHint:
        return FirewallHint(
            protocol="tcp",
            port=port,
            destination=host,
            reason="The Vantage backend requests the checked URL on this port.",
        )
