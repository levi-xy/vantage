"""``WS /api/v1/ws``: live pushes (AGENTS.md Contract 3, M5.3).

Authenticated by the same session cookie as the HTTP API (same expiry and idle rules,
re-checked while the socket is open). The Origin header must match the Host the browser
used, which stops cross-site WebSocket hijacking. The client can only send ``ping``.
"""

import asyncio
import logging
from datetime import timedelta
from urllib.parse import urlsplit

from fastapi import APIRouter, WebSocket
from sqlalchemy.orm import Session, sessionmaker
from starlette.concurrency import run_in_threadpool

from app.api.deps import cookie_name
from app.collectors.hub import EventHub, Subscriber
from app.core.auth import session_id
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


def origin_allowed(ws: WebSocket) -> bool:
    """Browsers always send Origin on WebSocket handshakes; no Origin means not a browser."""
    origin = ws.headers.get("origin")
    host = ws.headers.get("host")
    if not origin or not host:
        return False
    return urlsplit(origin).netloc.lower() == host.lower()


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
    if not origin_allowed(ws) or not token or len(token) > _MAX_TOKEN_LENGTH:
        await ws.close(CLOSE_POLICY)  # before accept: the handshake answers 403
        return
    if not await run_in_threadpool(
        session_valid, ws.app.state.session_factory, token, settings.session_idle_minutes
    ):
        await ws.close(CLOSE_POLICY)
        return
    sub = hub.subscribe()
    if sub is None:
        await ws.close(CLOSE_TRY_AGAIN)
        return
    await ws.accept()
    tasks = [
        asyncio.create_task(_sender(ws, sub)),
        asyncio.create_task(_receiver(ws, sub)),
        asyncio.create_task(_watcher(ws, sub, token)),
    ]
    code = CLOSE_POLICY
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            if not task.cancelled() and task.exception() is None:
                code = task.result() or CLOSE_POLICY
    finally:
        hub.unsubscribe(sub)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    try:
        await ws.close(code)
    except Exception:  # the peer already closed
        log.debug("websocket already closed")
