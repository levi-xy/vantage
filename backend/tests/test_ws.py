"""Live updates socket (M5.3)."""

import contextlib
import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, delete
from starlette.websockets import WebSocketDisconnect

from app.api import ws as ws_module
from app.collectors.hub import (
    MAX_CONNECTIONS_PER_SESSION,
    QUEUE_SIZE,
    EventHub,
    status_message,
)
from app.collectors.store import InstanceStatus
from app.core.db import make_session_factory
from app.models import UserSession
from tests.test_auth import do_setup, make_client

URL = "/api/v1/ws"
ORIGIN = {"origin": "https://testserver"}
INSTANCE_ID = "00000000-0000-0000-0000-000000000001"


@pytest.fixture
def client(engine: Engine, db_url: str) -> Iterator[TestClient]:
    with make_client(db_url) as c:
        do_setup(c)
        yield c


def connect(client: TestClient, **headers: str):  # type: ignore[no-untyped-def]
    """The test client doesn't send its cookie jar on WebSocket handshakes; do it by hand."""
    cookie = "; ".join(f"{c.name}={c.value}" for c in client.cookies.jar)
    return client.websocket_connect(URL, headers={**ORIGIN, "cookie": cookie, **headers})


def hub(client: TestClient) -> EventHub:
    h: EventHub = client.app.state.event_hub  # type: ignore[attr-defined]
    return h


def test_pushes_reach_a_logged_in_user_and_pings_are_answered(client: TestClient) -> None:
    with connect(client) as sock:
        sock.send_text("ping")
        assert sock.receive_json() == {"type": "pong"}
        status = InstanceStatus(state="down", error="no answer from 192.0.2.10")
        # Publishing happens on the app's event loop, as the poll scheduler does.
        assert client.portal is not None
        client.portal.call(hub(client).publish, status_message(uuid.UUID(INSTANCE_ID), status))
        msg = sock.receive_json()
    assert msg["type"] == "instance_status"
    assert msg["instance_id"] == INSTANCE_ID
    assert msg["status"] == {
        "state": "down",
        "checked_at": None,
        "last_success_at": None,
        "error": "no answer from 192.0.2.10",
    }


def test_refused_without_session_or_with_wrong_origin(client: TestClient) -> None:
    anonymous = TestClient(client.app, base_url="https://testserver")
    cases = [
        (anonymous, {}),  # no session
        (client, {"origin": "https://evil.example"}),  # another site
    ]
    for c, headers in cases:
        with pytest.raises(WebSocketDisconnect):
            with connect(c, **headers):
                pass
    cookie = "; ".join(f"{c.name}={c.value}" for c in client.cookies.jar)
    with pytest.raises(WebSocketDisconnect):  # no Origin at all
        with client.websocket_connect(URL, headers={"cookie": cookie}):
            pass


def test_listed_origin_is_accepted_when_the_proxy_rewrote_host(engine: Engine, db_url: str) -> None:
    with make_client(
        db_url, public_hosts=("testserver", "vantage.example.com", "10.0.0.5:8443")
    ) as c:
        do_setup(c)
        headers = {"origin": "https://vantage.example.com", "host": "10.0.0.5:8443"}
        with connect(c, **headers) as sock:
            sock.send_text("ping")
            assert sock.receive_json() == {"type": "pong"}
        with pytest.raises(WebSocketDisconnect):  # an unlisted Origin is still refused
            with connect(c, **{**headers, "origin": "https://evil.example"}):
                pass
        with pytest.raises(WebSocketDisconnect):  # and so is a missing one
            cookie = "; ".join(f"{k.name}={k.value}" for k in c.cookies.jar)
            with c.websocket_connect(URL, headers={"cookie": cookie, "host": "10.0.0.5:8443"}):
                pass


def test_origin_listing_matches_scheme_host_and_port_exactly(engine: Engine, db_url: str) -> None:
    with make_client(db_url, public_hosts=("testserver", "10.0.0.5")) as c:
        do_setup(c)
        host = {"host": "10.0.0.5:8443"}
        with connect(c, origin="https://10.0.0.5", **host):
            pass
        for origin in ("http://10.0.0.5:3000", "https://10.0.0.5:9443"):
            with pytest.raises(WebSocketDisconnect):
                with connect(c, origin=origin, **host):
                    pass
    assert ws_module._origin_listed("https://10.0.0.5:8443", ("10.0.0.5:8443",))
    assert not ws_module._origin_listed("https://10.0.0.5", ("10.0.0.5:8443",))


def test_refusal_logs_a_rate_limited_warning_without_the_cookie(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    ws_module._last_warned.clear()
    cookie = "; ".join(f"{c.name}={c.value}" for c in client.cookies.jar)
    secret = client.cookies.jar.__iter__().__next__().value
    assert secret
    for _ in range(3):
        with caplog.at_level("WARNING", logger="app.api.ws"):
            with pytest.raises(WebSocketDisconnect):
                with connect(client, origin="https://evil.example"):
                    pass
    lines = [r.getMessage() for r in caplog.records if "handshake refused" in r.getMessage()]
    assert len(lines) == 1  # once per reason per minute
    assert "evil.example" in lines[0] and "testserver" in lines[0]
    assert secret not in lines[0] and cookie not in lines[0]


def test_client_messages_other_than_ping_close_the_socket(client: TestClient) -> None:
    with connect(client) as sock:
        sock.send_text('{"subscribe": "everything"}')
        with pytest.raises(WebSocketDisconnect) as info:
            sock.receive_json()
    assert info.value.code == ws_module.CLOSE_POLICY


def test_socket_closes_when_the_session_ends(
    client: TestClient, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ws_module, "SESSION_RECHECK_SECONDS", 0.05)
    with connect(client) as sock:
        with make_session_factory(engine)() as db:
            db.execute(delete(UserSession))
            db.commit()
        with pytest.raises(WebSocketDisconnect) as info:
            sock.receive_json()
    assert info.value.code == ws_module.CLOSE_SESSION_ENDED


def test_slow_client_is_dropped_instead_of_growing_the_queue() -> None:
    h = EventHub()
    sub = h.subscribe()
    assert sub is not None
    message = {"type": "refresh", "instance_id": INSTANCE_ID}
    for _ in range(QUEUE_SIZE + 1):
        h.publish(message)
    assert sub.queue.qsize() == QUEUE_SIZE
    assert sub.dropped.is_set()
    h.publish(message)  # no longer subscribed
    assert sub.queue.qsize() == QUEUE_SIZE


def test_one_session_can_hold_only_a_few_sockets(client: TestClient) -> None:
    with contextlib.ExitStack() as stack:
        socks = [stack.enter_context(connect(client)) for _ in range(MAX_CONNECTIONS_PER_SESSION)]
        socks[-1].send_text("ping")
        assert socks[-1].receive_json() == {"type": "pong"}
        with connect(client) as extra:  # accepted, then closed before subscribing
            with pytest.raises(WebSocketDisconnect) as info:
                extra.receive_json()
        assert info.value.code == ws_module.CLOSE_TRY_AGAIN
        assert hub(client).subscriber_count == MAX_CONNECTIONS_PER_SESSION
    # Closed sockets give their slots back.
    with connect(client) as sock:
        sock.send_text("ping")
        assert sock.receive_json() == {"type": "pong"}


def test_hub_cap_is_per_key_and_released_once() -> None:
    h = EventHub()
    subs = [h.subscribe("a") for _ in range(MAX_CONNECTIONS_PER_SESSION)]
    assert all(subs) and h.subscribe("a") is None
    assert h.subscribe("b") is not None  # another session is unaffected
    first = subs[0]
    assert first is not None
    h.unsubscribe(first)
    h.unsubscribe(first)  # twice (dropped, then the handler's finally) frees one slot
    assert h.subscribe("a") is not None
    assert h.subscribe("a") is None
