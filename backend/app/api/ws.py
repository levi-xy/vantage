"""``WS /api/v1/ws``: live pushes (AGENTS.md Contract 3, M5.3).

Authenticated by the same session cookie as the HTTP API (same expiry and idle rules,
re-checked while the socket is open). The Origin header must match the Host the browser
used, or be a host the admin listed in ``VANTAGE_PUBLIC_HOSTS`` (a reverse proxy may
rewrite Host); this stops cross-site WebSocket hijacking. A refused handshake logs one
WARNING per reason per minute. The client can only send ``ping``.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable
from datetime import timedelta
from urllib.parse import urlsplit

import anyio
from fastapi import APIRouter, WebSocket
from sqlalchemy.orm import Session, sessionmaker
from starlette.concurrency import run_in_threadpool

from app.api.deps import cookie_name
from app.collectors.hub import EventHub, Subscriber
from app.core.auth import session_id
from app.core.config import host_allowed
from app.core.db import utcnow
from app.models import UserSession

log = logging.getLogger(__name__)
router = APIRouter()

SESSION_RECHECK_SECONDS = 30.0
MAX_CLIENT_MESSAGE = 64
_MAX_TOKEN_LENGTH = 128

CLOSE_POLICY = 1008
CLOSE_TRY_AGAIN = 1013
CLOSE_SESSION_ENDED = 4401  # app-defined: the session ended or expired


_WARN_EVERY_SECONDS = 60.0
_last_warned: dict[str, float] = {}


def _warn_refused(reason: str, ws: WebSocket) -> None:
    """One WARNING per reason per minute, so a reconnecting browser can't flood the log.

    Logs the Origin and Host values (host names, not secrets), never the cookie.
    """
    now = time.monotonic()
    last = _last_warned.get(reason)
    if last is not None and now - last < _WARN_EVERY_SECONDS:
        return
    _last_warned[reason] = now
    origin = urlsplit(ws.headers.get("origin") or "").netloc[:255]
    host = (ws.headers.get("host") or "")[:255]
    log.warning(
        "WebSocket handshake refused: %s (Origin host %r, Host header %r). Behind a reverse "
        "proxy, pass the original Host header and WebSocket upgrades, and list the name in "
        "VANTAGE_PUBLIC_HOSTS; see docs/OPERATIONS.md, Reverse proxy",
        reason,
        origin,
        host,
    )


def origin_refusal(ws: WebSocket, public_hosts: tuple[str, ...] = ()) -> str | None:
    """Why the Origin is refused, or None when it is fine.

    Browsers always send Origin on WebSocket handshakes; no Origin means not a browser.
    Accepted: the Origin's host is the Host the browser used, or is listed in
    ``VANTAGE_PUBLIC_HOSTS`` (same host / host:port rules as ``PublicHostGuard``).
    """
    origin = ws.headers.get("origin")
    host = ws.headers.get("host")
    if not origin:
        return "no Origin header"
    if not host:
        return "no Host header"
    netloc = urlsplit(origin).netloc.lower()
    if netloc == host.lower():
        return None
    if public_hosts and host_allowed(netloc, public_hosts):
        return None
    return "Origin doesn't match Host and isn't listed in VANTAGE_PUBLIC_HOSTS"


def session_valid(factory: sessionmaker[Session], token: str, idle_minutes: int) -> bool:
    """The HTTP rules: the row exists, isn't expired and isn't idle. Doesn't touch last_seen_at."""
    with factory() as db:
        row = db.get(UserSession, session_id(token))
        if row is None:
            return False
        now = utcnow()
        return row.expires_at > now and now - row.last_seen_at <= timedelta(minutes=idle_minutes)


async def _sender(ws: WebSocket, sub: Subscriber) -> int | None:
    while True:
        await ws.send_json(await sub.queue.get())


async def _receiver(ws: WebSocket, sub: Subscriber) -> int | None:
    while True:
        text = await ws.receive_text()
        if len(text) > MAX_CLIENT_MESSAGE or text.strip() != "ping":
            return CLOSE_POLICY
        if not sub.queue.full():
            sub.queue.put_nowait({"type": "pong"})


async def _watcher(ws: WebSocket, sub: Subscriber, token: str) -> int | None:
    """Returns the close code once the session ends or the client is too slow."""
    app = ws.app
    while True:
        try:
            await asyncio.wait_for(sub.dropped.wait(), SESSION_RECHECK_SECONDS)
            return CLOSE_TRY_AGAIN
        except TimeoutError:
            pass
        ok = await run_in_threadpool(
            session_valid,
            app.state.session_factory,
            token,
            app.state.settings.session_idle_minutes,
        )
        if not ok:
            return CLOSE_SESSION_ENDED


@router.websocket("/ws")
async def live(ws: WebSocket) -> None:
    settings = ws.app.state.settings
    hub: EventHub = ws.app.state.event_hub
    token = ws.cookies.get(cookie_name(settings))
    reason = origin_refusal(ws, settings.public_hosts)
    if reason is None and not token:
        reason = "no session cookie"
    if reason is None and token and len(token) > _MAX_TOKEN_LENGTH:
        reason = "oversized session cookie"
    if reason is not None or not token:
        _warn_refused(reason or "no session cookie", ws)
        await ws.close(CLOSE_POLICY)  # before accept: the handshake answers 403
        return
    if not await run_in_threadpool(
        session_valid, ws.app.state.session_factory, token, settings.session_idle_minutes
    ):
        _warn_refused("invalid or expired session", ws)
        await ws.close(CLOSE_POLICY)
        return
    await ws.accept()
    # Subscribe only once the handshake is done: an aborted handshake can't leave a
    # subscriber behind and use up the hub's connection cap.
    # Capped per session (keyed by the hashed session id, never the token itself).
    sub = hub.subscribe(session_id(token))
    if sub is None:
        await ws.close(CLOSE_TRY_AGAIN)
        return
    # The first of the three to finish decides the close code; the others are cancelled.
    # An anyio task group (not bare asyncio tasks) so cancellation from the server or the
    # test client's cancel scope reaches every part and none escapes it.
    code = CLOSE_POLICY

    async def first(coro: Awaitable[int | None], scope: anyio.CancelScope) -> None:
        nonlocal code
        try:
            result = await coro
            code = result or CLOSE_POLICY
        except Exception:  # the peer went away, or a send failed
            code = CLOSE_POLICY
        scope.cancel()

    try:
        async with anyio.create_task_group() as tg:
            tg.start_soon(first, _sender(ws, sub), tg.cancel_scope)
            tg.start_soon(first, _receiver(ws, sub), tg.cancel_scope)
            tg.start_soon(first, _watcher(ws, sub, token), tg.cancel_scope)
    finally:
        hub.unsubscribe(sub)
    try:
        await ws.close(code)
    except Exception:  # the peer already closed
        log.debug("websocket already closed")
