"""Live updates socket (M5.3)."""

import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, delete
from starlette.websockets import WebSocketDisconnect

from app.api import ws as ws_module
from app.collectors.hub import QUEUE_SIZE, EventHub, status_message
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
