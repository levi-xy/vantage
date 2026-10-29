"""Emby Server: now playing, transcode vs direct play, library counts and server info.

**Polled**: Vantage calls the Emby REST API on every poll through ``ctx.http_client()``,
the SSRF-guarded client, and exposes ``vantage_emby_*`` on its own ``/metrics``.

Read-only (Hard rule 2): only ``GET``, and only the paths in :data:`ALLOWED_PATHS`.
Every request goes through :func:`_get_json`, which refuses any other path before it is
sent. Nothing that plays, stops, messages, scans, deletes or changes anything is ever
called.

Secret: the API key, a top-level ``SecretStr``. It is sent in the ``X-Emby-Token``
header to the configured host and nowhere else: never in the query string, never in a
message or log line (redirects are not followed).

Privacy: the metrics are counts. The server's name is the only device text exported,
and only on the info series. No user name, client address, device name or media
title is read into a label or a message; the session fields that hold them are never
looked at.

TLS (https only): verification is on by default; the admin can pin a certificate, or
explicitly turn verification off. Plain http is allowed (Emby's default) but the API
key then crosses the network unencrypted; the docs say so.
"""

import asyncio
import json
import logging
import math
import re
import ssl
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, ClassVar, Self

import httpx
from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator

from app.core.net_guard import NetGuardError
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
    is_tls_failure,
)
from app.integrations.builtin.ifmib import MAX_LABEL_TEXT, clean_text

# Reuse the Proxmox pattern for pinning a pasted certificate (same parser, same limits).
from app.integrations.builtin.proxmox.integration import (
    MAX_CA_PEM_CHARS,
    _cached_pinned_context,
    _normalise_pem,
)
from app.integrations.errors import PollFailure

TYPE_ID = "emby"
PREFIX = f"vantage_{TYPE_ID}"

METRIC_INFO = f"{PREFIX}_server_info"  # value 1; labels: version (validated), server_name
METRIC_UPDATE = f"{PREFIX}_update_available"
METRIC_RESTART = f"{PREFIX}_restart_pending"
METRIC_SESSIONS = f"{PREFIX}_sessions"
METRIC_NOW_PLAYING = f"{PREFIX}_now_playing"
METRIC_STREAMS = f"{PREFIX}_streams"  # label: play_method
METRIC_ITEMS = f"{PREFIX}_items"  # label: item_type
METRIC_LIBRARIES = f"{PREFIX}_libraries"  # label: library_type

DEFAULT_PORT = 8096  # http; Emby's https default is 8920
MAX_TIMEOUT_SECONDS = 15
MAX_BODY_BYTES = 4 * 1024 * 1024
_PARSE_IN_THREAD_BYTES = 64 * 1024
# Four requests, each allowed up to 15 s, can pass the collector's cut-off: own budget.
POLL_BUDGET_SECONDS = 50.0
# Sessions older than this many seconds are not "active" (Emby's own parameter).
ACTIVE_WITHIN_SECONDS = 960
MAX_SESSIONS = 2000
MAX_LIBRARIES = 500

# Emby answers under /emby (the root also works, but /emby is the documented base).
PATH_INFO = "/emby/System/Info"
PATH_SESSIONS = f"/emby/Sessions?ActiveWithinSeconds={ACTIVE_WITHIN_SECONDS}"
PATH_COUNTS = "/emby/Items/Counts"
PATH_LIBRARIES = "/emby/Library/VirtualFolders"

# Every request the integration may send: GET, exactly these targets.
ALLOWED_PATHS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(re.escape(p)) for p in (PATH_INFO, PATH_SESSIONS, PATH_COUNTS, PATH_LIBRARIES)
)

_API_KEY_RE = re.compile(r"[!-~]{1,128}")  # printable ASCII, no spaces (it is a header value)
# Versions look like 4.8.10.0, 4.9.0.42 or 4.9.1.0-beta. Anything else is not exported.
_VERSION_RE = re.compile(r"[0-9][0-9A-Za-z.+_-]{0,31}")

# Item counts the API reports, mapped to fixed label values (never device text).
ITEM_TYPES: dict[str, str] = {
    "MovieCount": "movie",
    "SeriesCount": "series",
    "EpisodeCount": "episode",
    "AlbumCount": "album",
    "SongCount": "song",
    "ArtistCount": "artist",
    "MusicVideoCount": "music_video",
    "BoxSetCount": "box_set",
    "TrailerCount": "trailer",
    "ProgramCount": "program",
    "GameCount": "game",
    "BookCount": "book",
}
# Library (virtual folder) collection types, mapped to fixed label values.
LIBRARY_TYPES = {
    "movies": "movies",
    "tvshows": "tvshows",
    "music": "music",
    "musicvideos": "musicvideos",
    "homevideos": "homevideos",
    "photos": "photos",
    "books": "books",
    "games": "games",
    "boxsets": "boxsets",
    "playlists": "playlists",
    "livetv": "livetv",
}
PLAY_METHODS = {
    "directplay": "direct_play",
    "directstream": "direct_stream",
    "transcode": "transcode",
}

# Shown after a successful test over plain http (Emby's default): the key is not encrypted.
PLAIN_HTTP_NOTE = "(API key sent over plain http; enable HTTPS on the server to protect it)"

logger = logging.getLogger(__name__)


# --- failures --------------------------------------------------------------------------


class EmbyAuthFailed(PollFailure):
    """HTTP 401: the API key is wrong or was revoked."""


class EmbyForbidden(PollFailure):
    """HTTP 403: the key is valid but not allowed to read a required path."""


class EmbyTlsUntrusted(PollFailure):
    """The TLS handshake failed: the certificate is not trusted, or the host doesn't match."""


class EmbyUnreachable(PollFailure):
    """The connection was refused or the host is unreachable."""


class EmbyTimeout(PollFailure):
    """A request got no answer within the configured timeout."""


class EmbyRequestFailed(PollFailure):
    """Any other HTTP failure (an unexpected status, a broken connection)."""


class EmbyBadAnswer(PollFailure):
    """The answer was not Emby's JSON (wrong port, a proxy, too large, unexpected shape)."""


class EmbyPollTooSlow(PollFailure):
    """The whole poll took longer than :data:`POLL_BUDGET_SECONDS`."""


class _HttpStatus(Exception):
    """A non-200 answer. Internal: callers turn it into a message they write."""

    def __init__(self, status: int) -> None:
        super().__init__(status)
        self.status = status


class _NotTheApi(Exception):
    """The body was not the JSON the Emby API sends. Internal."""


# --- config ----------------------------------------------------------------------------


class EmbyConfig(BaseModel):
    """What the admin enters. The JSON schema of this model drives the UI form."""

    host: str = Field(
        title="Host",
        description=(
            "IP address or DNS name of the Emby server. Must be on a private network "
            "(RFC 1918, CGNAT or IPv6 ULA). Stored in plaintext and shown to every user."
        ),
        max_length=253,
        examples=["10.0.0.60"],
    )
    port: int = Field(
        default=DEFAULT_PORT,
        ge=1,
        le=65535,
        title="Port",
        description="Emby's defaults are 8096 (http) and 8920 (https).",
    )
    use_https: bool = Field(
        default=False,
        title="Use HTTPS",
        description=(
            "Emby serves plain http by default; then the API key crosses the network "
            "unencrypted. Prefer HTTPS (own certificate, or behind a reverse proxy)."
        ),
    )
    api_key: SecretStr = Field(
        title="API key",
        description=(
            "An Emby API key (Dashboard, Advanced, API Keys). Create one just for Vantage "
            "so you can revoke it without touching anything else."
        ),
    )
    tls_ca_certificate: str | None = Field(
        default=None,
        title="Trusted certificate (PEM)",
        description=(
            "HTTPS only. For a self-signed or private-CA certificate: paste the CA "
            "certificate (or the server's own). Only it is then trusted, and the host "
            "must still match. Leave empty for a publicly trusted certificate. Never paste "
            "a private key: this field is stored in plaintext."
        ),
        max_length=MAX_CA_PEM_CHARS,
    )
    tls_insecure_skip_verify: bool = Field(
        default=False,
        title="INSECURE: don't verify the TLS certificate",
        description=(
            "HTTPS only. Not recommended: sends the API key to whatever answers on this "
            "address, without checking who it is. Use the trusted certificate field "
            "instead; turn this on only as a last resort, on a network you control."
        ),
    )
    timeout_seconds: int = Field(
        default=10, ge=1, le=MAX_TIMEOUT_SECONDS, title="Timeout per request (seconds)"
    )

    @field_validator("host")
    @classmethod
    def _check_host(cls, value: str) -> str:
        return checked_host(value)

    @field_validator("api_key")
    @classmethod
    def _check_api_key(cls, value: SecretStr) -> SecretStr:
        # Never echo the value. Printable ASCII without spaces also keeps it from
        # breaking out of the header.
        if not _API_KEY_RE.fullmatch(value.get_secret_value()):
            raise ValueError(
                "the API key must be printable ASCII without spaces, at most 128 characters"
            )
        return value

    @field_validator("tls_ca_certificate")
    @classmethod
    def _check_ca(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        return _normalise_pem(value)

    @model_validator(mode="after")
    def _tls_choices(self) -> Self:
        if self.tls_ca_certificate is not None and self.tls_insecure_skip_verify:
            raise ValueError(
                "choose either a trusted certificate or not verifying the certificate, not both"
            )
        if not self.use_https and (self.tls_ca_certificate or self.tls_insecure_skip_verify):
            raise ValueError("the TLS settings apply only when 'Use HTTPS' is on")
        return self

    def where(self) -> str:
        return host_port(self.host, self.port)

    def tls_verify(self) -> bool | ssl.SSLContext:
        """What the guarded client verifies the server certificate against."""
        if self.tls_insecure_skip_verify:
            return False
        if self.tls_ca_certificate is None:
            return True
        return _cached_pinned_context(self.tls_ca_certificate)


# --- requests --------------------------------------------------------------------------


def allowed_path(target: str) -> bool:
    """Whether ``target`` (path and query) is one the integration may request."""
    return any(pattern.fullmatch(target) for pattern in ALLOWED_PATHS)


def _client(ctx: InstanceContext, cfg: EmbyConfig) -> httpx.AsyncClient:
    # The key is read here, at the point of use, and goes only into this header, to the
    # configured host. Redirects are not followed, so it can't follow one elsewhere.
    scheme = "https" if cfg.use_https else "http"
    return ctx.http_client(
        verify=cfg.tls_verify(),
        base_url=f"{scheme}://{cfg.where()}",
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "X-Emby-Token": cfg.api_key.get_secret_value(),
        },
    )


async def _read_limited(response: httpx.Response) -> bytes | None:
    body = bytearray()
    async for chunk in response.aiter_bytes():
        body += chunk
        if len(body) > MAX_BODY_BYTES:
            return None
    return bytes(body)


async def _get_json(client: httpx.AsyncClient, path: str, timeout: float) -> Any:  # noqa: ASYNC109
    """GET an allowlisted path and return its parsed JSON.

    Raises :class:`_HttpStatus` for a non-200 answer, :class:`_NotTheApi` for a body
    that isn't JSON, and lets transport errors through.
    """
    if not allowed_path(path):
        # Defence in depth: every caller builds its path from the constants above.
        raise ValueError("not an allowlisted Emby API path")
    async with asyncio.timeout(timeout), client.stream("GET", path) as response:
        if response.status_code != 200:
            raise _HttpStatus(response.status_code)
        body = await _read_limited(response)
    if body is None:
        raise _NotTheApi("too large")
    try:
        if len(body) > _PARSE_IN_THREAD_BYTES:
            return await asyncio.to_thread(json.loads, body)
        return json.loads(body)
    except (ValueError, RecursionError):
        raise _NotTheApi("not JSON") from None


# --- parsing ---------------------------------------------------------------------------
# Parsers raise only _NotTheApi (wrong top-level shape); odd inner values are skipped.


def _count(value: Any) -> int | None:
    """A non-negative whole number from the API, else ``None``."""
    if isinstance(value, bool):
        return None
    if isinstance(value, float) and math.isfinite(value) and value >= 0 and value == int(value):
        value = int(value)
    return value if isinstance(value, int) and 0 <= value <= 10**12 else None


def parse_info(data: Any) -> str | None:
    """``GET /System/Info``: the version if it looks like one, else ``None``."""
    if not isinstance(data, dict):
        raise _NotTheApi("info is not an object")
    version = data.get("Version")
    return version if isinstance(version, str) and _VERSION_RE.fullmatch(version) else None


def info_samples(data: dict[str, Any], version: str | None) -> list[Sample]:
    # The server name is device text: only on this info series (value 1), cleaned and
    # capped (maintainer's label rule, INTEGRATIONS.md section 9).
    samples = [
        Sample(
            name=METRIC_INFO,
            labels={
                "version": version or "",
                "server_name": clean_text(data.get("ServerName"), MAX_LABEL_TEXT),
            },
            value=1.0,
            type="gauge",
        )
    ]
    for metric, key in (
        (METRIC_UPDATE, "HasUpdateAvailable"),
        (METRIC_RESTART, "HasPendingRestart"),
    ):
        flag = data.get(key)
        if isinstance(flag, bool):
            samples.append(Sample(name=metric, value=1.0 if flag else 0.0, type="gauge"))
    return samples


def session_samples(data: Any) -> list[Sample]:
    """``GET /Sessions``: counts of sessions and of what is playing, by play method.

    A session is "playing" if it has a ``NowPlayingItem``. Its ``PlayState.PlayMethod``
    says transcode, direct stream or direct play. User, device, client address and
    title fields are never read.
    """
    if not isinstance(data, list):
        raise _NotTheApi("sessions is not a list")
    entries = [s for s in data[:MAX_SESSIONS] if isinstance(s, dict)]
    methods = {name: 0 for name in (*PLAY_METHODS.values(), "unknown")}
    playing = 0
    for session in entries:
        item = session.get("NowPlayingItem")
        if not isinstance(item, dict):
            continue
        playing += 1
        state = session.get("PlayState")
        raw = state.get("PlayMethod") if isinstance(state, dict) else None
        key = PLAY_METHODS.get(raw.lower()) if isinstance(raw, str) else None
        methods[key or "unknown"] += 1
    samples = [
        Sample(name=METRIC_SESSIONS, value=float(len(entries)), type="gauge"),
        Sample(name=METRIC_NOW_PLAYING, value=float(playing), type="gauge"),
    ]
    samples += [
        Sample(name=METRIC_STREAMS, labels={"play_method": m}, value=float(n), type="gauge")
        for m, n in methods.items()
    ]
    return samples


def count_samples(data: Any) -> list[Sample]:
    """``GET /Items/Counts``: item totals by kind (fixed labels)."""
    if not isinstance(data, dict):
        raise _NotTheApi("counts is not an object")
    samples: list[Sample] = []
    for key, label in ITEM_TYPES.items():
        number = _count(data.get(key))
        if number is not None:
            samples.append(
                Sample(
                    name=METRIC_ITEMS,
                    labels={"item_type": label},
                    value=float(number),
                    type="gauge",
                )
            )
    return samples


def library_samples(data: Any) -> list[Sample]:
    """``GET /Library/VirtualFolders``: how many libraries of each type. Names and
    paths are never read."""
    if not isinstance(data, list):
        raise _NotTheApi("libraries is not a list")
    totals = {label: 0 for label in (*LIBRARY_TYPES.values(), "other")}
    for entry in data[:MAX_LIBRARIES]:
        if not isinstance(entry, dict):
            continue
        kind = entry.get("CollectionType")
        label = LIBRARY_TYPES.get(kind.lower()) if isinstance(kind, str) else None
        totals[label or "other"] += 1
    return [
        Sample(name=METRIC_LIBRARIES, labels={"library_type": k}, value=float(n), type="gauge")
        for k, n in totals.items()
        if n or k != "other"
    ]


# --- messages --------------------------------------------------------------------------
# Fixed text plus the host:port as configured: safe to show (see PollFailure).


def _msg_auth(where: str) -> str:
    return (
        f"Emby at {where} refused the API key (HTTP 401): check the key, and that it "
        "hasn't been revoked"
    )


def _msg_forbidden(where: str) -> str:
    return f"the API key is not allowed to read {where} (HTTP 403)"


def _msg_tls(where: str) -> str:
    return (
        f"certificate not trusted: the TLS certificate of {where} could not be verified. "
        "Paste the CA certificate into 'Trusted certificate', and make sure the host is a "
        "name or address the certificate covers"
    )


def _msg_status(where: str, status: int) -> str:
    return f"Emby at {where} answered HTTP {status}"


def _msg_bad_answer(where: str) -> str:
    return (
        f"{where} answered, but not like the Emby API (check the port and the 'Use HTTPS' "
        "setting, and that nothing sits in between)"
    )


@contextmanager
def _poll_failures(where: str) -> Iterator[None]:
    """Turn request and parse failures into PollFailures with fixed, secret-free
    messages. No exception's own text is shown. Guard refusals pass through."""
    try:
        yield
    except NetGuardError:
        raise
    except _HttpStatus as exc:
        if exc.status == 401:
            raise EmbyAuthFailed(_msg_auth(where)) from None
        if exc.status == 403:
            raise EmbyForbidden(_msg_forbidden(where)) from None
        raise EmbyRequestFailed(_msg_status(where, exc.status)) from None
    except _NotTheApi:
        raise EmbyBadAnswer(_msg_bad_answer(where)) from None
    except (TimeoutError, httpx.TimeoutException):
        raise EmbyTimeout(f"timed out waiting for {where}") from None
    except httpx.ConnectError as exc:
        if is_tls_failure(exc):
            raise EmbyTlsUntrusted(_msg_tls(where)) from None
        raise EmbyUnreachable(f"could not connect to {where} (refused or unreachable)") from None
    except httpx.HTTPError as exc:
        raise EmbyRequestFailed(f"request to {where} failed ({type(exc).__name__})") from None


async def _optional(client: httpx.AsyncClient, path: str, timeout: float) -> Any:  # noqa: ASYNC109
    """An extra request: ``None`` if it was answered with any error status or isn't
    JSON. Transport failures still raise."""
    try:
        return await _get_json(client, path, timeout)
    except (_HttpStatus, _NotTheApi):
        return None


# --- the integration -------------------------------------------------------------------


class Emby(Integration):
    type_id = TYPE_ID
    display_name = "Emby"
    category: ClassVar[Category] = "media"
    version = "0.1.0"
    config_schema = EmbyConfig

    async def test_connection(self, ctx: InstanceContext) -> TestResult:
        """``GET /System/Info`` (is it Emby, is the key valid), then ``GET /Sessions``."""
        cfg = ctx.config_as(EmbyConfig)
        where = cfg.where()
        timeout = float(cfg.timeout_seconds)
        try:
            async with _client(ctx, cfg) as client:
                info = await _get_json(client, PATH_INFO, timeout)
                version = parse_info(info)
                sessions = await _get_json(client, PATH_SESSIONS, timeout)
                active = int(session_samples(sessions)[0].value)  # first sample: sessions
        except _HttpStatus as exc:
            if exc.status == 401:
                return TestResult(ok=False, message=_msg_auth(where))
            if exc.status == 403:
                return TestResult(ok=False, message=_msg_forbidden(where))
            return TestResult(
                ok=False,
                message=f"{_msg_status(where, exc.status)} (is this the Emby port?)",
            )
        except (_NotTheApi, ValueError, TypeError, KeyError, AttributeError):
            return TestResult(ok=False, message=_msg_bad_answer(where))
        except httpx.ConnectError as exc:
            if is_tls_failure(exc):
                return TestResult(ok=False, message=_msg_tls(where))
            return connection_failure(exc, where=where, hint=self._hint(cfg))
        except Exception as exc:
            return connection_failure(exc, where=where, hint=self._hint(cfg))
        label = f"Emby {version}" if version else "Emby"
        plural = "" if active == 1 else "s"
        note = "" if cfg.use_https else f" {PLAIN_HTTP_NOTE}"
        return TestResult(ok=True, message=f"{label}: {active} active session{plural}{note}")

    async def poll(self, ctx: InstanceContext) -> list[Sample]:
        """Info and sessions are required (a failure marks the instance ``down`` with a
        short message); item counts and libraries are optional and left out if the
        server refuses them."""
        cfg = ctx.config_as(EmbyConfig)
        where = cfg.where()
        timeout = float(cfg.timeout_seconds)
        samples: list[Sample] = []
        try:
            async with asyncio.timeout(POLL_BUDGET_SECONDS):
                with _poll_failures(where):
                    async with _client(ctx, cfg) as client:
                        info = await _get_json(client, PATH_INFO, timeout)
                        samples += info_samples(info, parse_info(info))
                        samples += session_samples(await _get_json(client, PATH_SESSIONS, timeout))
                        for path, parse in (
                            (PATH_COUNTS, count_samples),
                            (PATH_LIBRARIES, library_samples),
                        ):
                            extra = await _optional(client, path, timeout)
                            if extra is None:
                                continue
                            try:
                                samples += parse(extra)
                            except _NotTheApi:
                                logger.warning(
                                    "emby instance %s: an optional answer had an unexpected shape",
                                    ctx.instance_id,
                                )
        except TimeoutError:
            raise EmbyPollTooSlow(
                f"Emby poll of {where} took longer than {POLL_BUDGET_SECONDS:g} s"
            ) from None
        return samples

    def poll_interval_seconds(self) -> int:
        return 30

    def cards(self) -> list[CardSpec]:
        return [
            CardSpec(card_type="emby-overview", title="Emby", default_size=(4, 3)),
            CardSpec(card_type="emby-libraries", title="Emby libraries", default_size=(4, 2)),
        ]

    def firewall_hints(self, ctx: InstanceContext) -> list[FirewallHint]:
        return [self._hint(ctx.config_as(EmbyConfig))]

    @staticmethod
    def _hint(cfg: EmbyConfig) -> FirewallHint:
        scheme = "HTTPS" if cfg.use_https else "HTTP"
        return FirewallHint(
            protocol="tcp",
            port=cfg.port,
            destination=cfg.host,
            reason=f"The Vantage backend calls the Emby API ({scheme}) on this port.",
        )
