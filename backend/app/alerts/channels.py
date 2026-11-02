"""Alert channels (M7.2): Telegram.

A channel's settings are split like an instance's (Contract 2): the ``SecretStr`` fields
of its schema (the bot token) are encrypted into ``channels.secrets``; everything else
(the chat id) is stored in ``channels.config``.

Sending: ``POST https://api.telegram.org/bot<token>/sendMessage`` through the SSRF guard,
with ``api.telegram.org`` as the only declared cloud host. The guard only lets a cloud
host through over HTTPS with certificate and hostname verification, and only when it
resolves to public addresses; redirects are not followed; every request has the guard's
timeouts.

The bot token is part of the URL path, so it must never reach a log line, an exception
message, an audit entry or an API response:

* errors are built here from fixed text, the exception *type* and Telegram's own short
  description, then scrubbed of the token; an httpx exception's text (which can hold the
  URL) is never used, and is not chained (``from None``);
* httpx logs every request URL at INFO level; :class:`_TokenLogFilter` on the ``httpx``
  logger rewrites ``/bot<token>`` to ``/bot<redacted>`` in those records;
* the token's format is validated, so it can't carry ``/``, ``?`` or ``#`` and change
  which path is requested.
"""

import logging
import re
from collections.abc import Callable, Sequence
from typing import Annotated

import httpx
from pydantic import BaseModel, ConfigDict, SecretStr, StringConstraints, field_validator

from app.core.crypto import SecretBox, row_context
from app.core.net_guard import NetGuard, NetGuardError, NetPolicy, guarded_http_client
from app.models import Channel, ChannelType

log = logging.getLogger(__name__)

CHANNELS_TABLE = "channels"
TELEGRAM_API_HOST = "api.telegram.org"
CLOUD_HOSTS: tuple[str, ...] = (TELEGRAM_API_HOST,)
# Telegram's limit for one message's text, in characters (counted as UTF-16 code units).
MAX_TEXT_LENGTH = 4096
TEST_MESSAGE = "Vantage test message: this channel can deliver alerts."
_REDACTED = "<redacted>"
_ELLIPSIS = "…"
_MAX_DESCRIPTION = 200

# Bot tokens look like "123456789:AA...". Only these characters, so the token can't
# change the request path (no "/", "?", "#", "%" or whitespace).
_BOT_TOKEN = re.compile(r"\d{1,20}:[A-Za-z0-9_-]{20,128}")
# A numeric chat id (groups and channels are negative) or a public "@channelname".
_CHAT_ID = re.compile(r"-?\d{1,20}|@[A-Za-z][A-Za-z0-9_]{3,31}")
_TOKEN_IN_PATH = re.compile(r"/bot[^/\s\"']+")


class TelegramConfig(BaseModel):
    """A Telegram channel: the bot's token (secret) and the chat it posts to."""

    model_config = ConfigDict(extra="forbid")

    chat_id: Annotated[str, StringConstraints(strip_whitespace=True, pattern=_CHAT_ID.pattern)]
    bot_token: SecretStr

    @field_validator("bot_token", mode="after")
    @classmethod
    def _token_format(cls, value: SecretStr) -> SecretStr:
        # After SecretStr wrapping (not a pattern on a str), so errors can't echo it.
        if not _BOT_TOKEN.fullmatch(value.get_secret_value()):
            raise ValueError("must look like 123456789:ABC... (the token from @BotFather)")
        return value


# The schema of each channel type's settings.
CHANNEL_SCHEMAS: dict[ChannelType, type[BaseModel]] = {ChannelType.TELEGRAM: TelegramConfig}


class SendError(Exception):
    """A message wasn't delivered. The message holds no secret.

    ``kind`` is a fixed code for the delivery outbox (``timeout``, ``network``,
    ``rate_limited``, ``http_4xx``, ``http_5xx``, ``unauthorized``, ``forbidden``,
    ``chat_not_found``, ``bad_response``); ``permanent`` means retrying can't help;
    ``retry_after`` is Telegram's requested wait in seconds on a 429.
    """

    def __init__(
        self,
        message: str,
        *,
        kind: str = "network",
        permanent: bool = False,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.permanent = permanent
        self.retry_after = retry_after


# --- the token never reaches a log line ------------------------------------------------


def scrub(text: str, secrets: Sequence[str] = ()) -> str:
    """``text`` without any ``/bot<token>`` path or any of ``secrets``."""
    text = _TOKEN_IN_PATH.sub(f"/bot{_REDACTED}", text)
    for value in sorted((s for s in secrets if s), key=len, reverse=True):
        text = text.replace(value, _REDACTED)
    return text


class _TokenLogFilter(logging.Filter):
    """Rewrites ``/bot<token>`` in a record's final message (httpx logs request URLs)."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # a malformed record: let logging report it as usual
            return True
        if "/bot" in message:
            record.msg = scrub(message)
            record.args = None
        return True


# A logger's filters don't apply to its children's records, so every logger is listed.
_HTTP_LOGGERS = (
    "httpx",
    "httpcore",
    "httpcore.connection",
    "httpcore.http11",
    "httpcore.http2",
    "httpcore.proxy",
    "httpcore.socks",
)


def _install_log_filter() -> None:
    for name in _HTTP_LOGGERS:
        logger = logging.getLogger(name)
        if not any(isinstance(f, _TokenLogFilter) for f in logger.filters):
            logger.addFilter(_TokenLogFilter())


_install_log_filter()


# --- sending ---------------------------------------------------------------------------

ClientFactory = Callable[[], httpx.AsyncClient]


def telegram_guard() -> NetGuard:
    """The default policy plus Telegram's API host as the only cloud host."""
    return NetGuard(NetPolicy(), cloud_hosts=CLOUD_HOSTS)


def default_client_factory() -> httpx.AsyncClient:
    """A guarded client with certificate verification on (required for a cloud host)."""
    return guarded_http_client(telegram_guard(), verify=True)


def cap_text(text: str) -> str:
    """At most :data:`MAX_TEXT_LENGTH` UTF-16 code units, ending in "…" when cut."""
    if len(text.encode("utf-16-le")) // 2 <= MAX_TEXT_LENGTH:
        return text if text.strip() else "(empty message)"
    out: list[str] = []
    used = 1  # the ellipsis
    for ch in text:
        size = 2 if ord(ch) > 0xFFFF else 1
        if used + size > MAX_TEXT_LENGTH:
            break
        out.append(ch)
        used += size
    return "".join(out) + _ELLIPSIS


def _answer(response: httpx.Response) -> dict[str, object]:
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _description(data: dict[str, object]) -> str:
    text = data.get("description")
    if not isinstance(text, str):
        return ""
    return " ".join(text.split())[:_MAX_DESCRIPTION]


def _retry_after(data: dict[str, object]) -> float | None:
    params = data.get("parameters")
    value = params.get("retry_after") if isinstance(params, dict) else None
    if isinstance(value, int | float) and not isinstance(value, bool) and value >= 0:
        return float(value)
    return None


# Telegram answers that retrying cannot fix: 400 (bad request, e.g. chat not found), 401
# (bad token), 403 (bot blocked or removed from the chat), and 404, which is how Telegram
# answers a malformed or revoked bot token (the bot URL does not exist): "unauthorized".
_PERMANENT_STATUS = {400: "http_4xx", 401: "unauthorized", 403: "forbidden", 404: "unauthorized"}


def _http_error(status: int, data: dict[str, object]) -> tuple[str, bool]:
    """(error kind, permanent) for a non-200 answer."""
    if status == 429:
        return "rate_limited", False
    if status in _PERMANENT_STATUS:
        if "chat not found" in _description(data).lower():
            return "chat_not_found", True
        return _PERMANENT_STATUS[status], True
    if status >= 500:
        return "http_5xx", False
    return "http_4xx", False


async def send_telegram(client: httpx.AsyncClient, config: TelegramConfig, text: str) -> None:
    """Send ``text`` (plain text, capped) to the chat. Raises :class:`SendError`."""
    token = config.bot_token.get_secret_value()
    url = f"https://{TELEGRAM_API_HOST}/bot{token}/sendMessage"
    # Plain text: no parse_mode, so alert text is never interpreted as markup.
    payload = {
        "chat_id": config.chat_id,
        "text": cap_text(text),
        "link_preview_options": {"is_disabled": True},
    }
    try:
        response = await client.post(url, json=payload)
    except NetGuardError as exc:  # names the host and the reason, never the path
        raise SendError(scrub(f"refused by the network guard: {exc}", [token])) from None
    except httpx.TimeoutException:
        raise SendError("Telegram API timed out", kind="timeout") from None
    except httpx.HTTPError as exc:
        # Never str(exc): httpx exceptions can carry the request URL, token included.
        raise SendError(f"Telegram API unreachable ({type(exc).__name__})") from None
    data = _answer(response)
    if response.status_code == 200:
        if data.get("ok") is True:
            return
        raise SendError("Telegram API gave an unexpected answer", kind="bad_response")
    detail = _description(data)
    message = f"Telegram API answered HTTP {response.status_code}"
    kind, permanent = _http_error(response.status_code, data)
    raise SendError(
        scrub(f"{message}: {detail}" if detail else message, [token]),
        kind=kind,
        permanent=permanent,
        retry_after=_retry_after(data) if kind == "rate_limited" else None,
    )


# --- stored channels -------------------------------------------------------------------


def encrypt(box: SecretBox, channel_id: int, secrets: dict[str, str]) -> bytes | None:
    if not secrets:
        return None
    return box.encrypt_secrets(secrets, context=row_context(CHANNELS_TABLE, channel_id))


def decrypt(box: SecretBox, channel_id: int, blob: bytes | None) -> dict[str, str]:
    """Raises ``DecryptionError`` (whose message holds nothing secret) on a bad blob."""
    if blob is None:
        return {}
    return box.decrypt_secrets(blob, context=row_context(CHANNELS_TABLE, channel_id))


def load_config(box: SecretBox, row: Channel) -> BaseModel:
    """The channel's full config (secrets decrypted). Raises ``ValueError`` or
    ``DecryptionError``; neither message holds a secret."""
    schema = CHANNEL_SCHEMAS[row.type]
    merged = {**row.config, **decrypt(box, row.id, row.secrets)}
    try:
        return schema.model_validate(merged)
    except ValueError:
        raise ValueError("the stored channel settings are no longer valid") from None


async def deliver(client: httpx.AsyncClient, config: BaseModel, text: str) -> None:
    """Send through the channel's type. Raises :class:`SendError`."""
    if isinstance(config, TelegramConfig):
        await send_telegram(client, config, text)
        return
    raise SendError("unsupported channel type")  # pragma: no cover - one type so far
