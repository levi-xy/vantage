"""Alert channels (M7.2): CRUD, the Telegram sender, the notifier.

Security assertions: the bot token never appears in a response body, a log line, an
exception or the audit log; sending goes through the real SSRF guard to the declared
cloud host only, over verified HTTPS. Only the socket layer is fake (``FakeNetwork``).
``api.telegram.org`` resolves to a TEST-NET-1 address that these tests treat as public.
"""

import json
import logging
import ssl
from collections.abc import Iterator
from ipaddress import IPv4Network
from typing import Any

import httpcore
import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.alerts import channels as ch
from app.api.channels import get_client_factory
from app.api.deps import CSRF_HEADER
from app.core import net_guard
from app.core.crypto import SecretBox, row_context
from app.core.db import make_session_factory
from app.core.net_guard import NetGuard, NetPolicy
from app.models import AuditLog, Channel, Role
from tests.conftest import TEST_MASTER_KEY
from tests.integrations.netfake import DNS, FakeNetwork, FakeRequest, fake_resolver, http_response
from tests.test_auth import add_user, do_setup, login, make_client

API = "/api/v1"
HOST = "api.telegram.org"
ADDRESS = "192.0.2.90"  # TEST-NET-1, treated as public by public_test_net
TOKEN = "123456789:example-token-telegram-00000000"
NEW_TOKEN = "987654321:example-token-telegram-11111111"
SECRETS = (TOKEN, NEW_TOKEN, "example-token-telegram")
CHAT = "-1001234567890"


@pytest.fixture(autouse=True)
def public_test_net(monkeypatch: pytest.MonkeyPatch) -> None:
    test_net = IPv4Network("192.0.2.0/24")
    original = net_guard._is_public
    monkeypatch.setattr(net_guard, "_is_public", lambda addr: addr in test_net or original(addr))
    monkeypatch.setitem(DNS, HOST, [ADDRESS])
    monkeypatch.setitem(DNS, "api.example.com", ["192.0.2.91"])


@pytest.fixture
def network() -> FakeNetwork:
    return FakeNetwork()


def factory_for(network: FakeNetwork) -> ch.ClientFactory:
    """The production client (real guard, real transport) on the fake socket layer."""

    def make() -> httpx.AsyncClient:
        guard = NetGuard(
            NetPolicy(own_addresses=frozenset()), cloud_hosts=ch.CLOUD_HOSTS, resolver=fake_resolver
        )
        return net_guard.guarded_http_client(guard, network_backend=network)

    return make


def telegram(status: int = 200, body: dict[str, Any] | None = None) -> bytes:
    payload = body if body is not None else {"ok": True, "result": {"message_id": 1}}
    return http_response(status, json.dumps(payload).encode(), reason="X")


def assert_no_secrets(text: str) -> None:
    for value in SECRETS:
        assert value not in text
    assert "**********" not in text


@pytest.fixture
def admin(engine: Engine, db_url: str, network: FakeNetwork) -> Iterator[TestClient]:
    with make_client(db_url) as c:
        c.app.dependency_overrides[get_client_factory] = lambda: factory_for(network)  # type: ignore[attr-defined]
        me = do_setup(c)
        c.headers[CSRF_HEADER] = me["csrf_token"]
        yield c


def create(client: TestClient, **config: Any) -> dict[str, Any]:
    body = {"type": "telegram", "name": "ops", "config": {"chat_id": CHAT, "bot_token": TOKEN}}
    body["config"].update(config)  # type: ignore[union-attr]
    r = client.post(f"{API}/channels", json=body)
    assert r.status_code == 201, r.text
    assert_no_secrets(r.text)
    data: dict[str, Any] = r.json()
    return data


def stored_token(session: Session, channel_id: int) -> str:
    session.expire_all()
    row = session.get(Channel, channel_id)
    assert row is not None and row.secrets is not None
    assert TOKEN.encode() not in row.secrets and "bot_token" not in row.config
    values = SecretBox(TEST_MASTER_KEY).decrypt_secrets(
        row.secrets, context=row_context("channels", channel_id)
    )
    return values["bot_token"]


# --- CRUD --------------------------------------------------------------------------------


def test_crud_keeps_the_token_write_only(admin: TestClient, session: Session) -> None:
    data = create(admin)
    assert data["type"] == "telegram" and data["name"] == "ops"
    assert data["config"] == {"chat_id": CHAT}
    assert data["secrets_set"] == {"bot_token": True}
    cid = data["id"]
    assert stored_token(session, cid) == TOKEN

    texts = [admin.get(f"{API}/channels").text, admin.get(f"{API}/channels/{cid}").text]

    # Omitted token: unchanged.
    r = admin.patch(
        f"{API}/channels/{cid}", json={"name": "ops-2", "config": {"chat_id": "@vantage_ops"}}
    )
    assert r.status_code == 200, r.text
    texts.append(r.text)
    assert r.json()["config"] == {"chat_id": "@vantage_ops"}
    assert stored_token(session, cid) == TOKEN

    r = admin.patch(f"{API}/channels/{cid}", json={"config": {"bot_token": NEW_TOKEN}})
    assert r.status_code == 200
    texts.append(r.text)
    assert stored_token(session, cid) == NEW_TOKEN

    assert admin.delete(f"{API}/channels/{cid}").status_code == 204
    assert admin.get(f"{API}/channels/{cid}").status_code == 404
    for text in texts:
        assert_no_secrets(text)

    session.expire_all()
    rows = list(session.scalars(select(AuditLog).where(AuditLog.target_type == "channel")))
    assert [r.action for r in rows] == ["create", "update", "update", "delete"]
    assert rows[0].changes["config"] == {"chat_id": CHAT, "bot_token": "<changed>"}
    assert "bot_token" not in rows[1].changes.get("config", {})
    assert rows[2].changes["config"] == {"bot_token": "<changed>"}
    assert_no_secrets(json.dumps([r.changes for r in rows]))


@pytest.mark.parametrize(
    "config",
    [
        {"chat_id": "not a chat", "bot_token": TOKEN},
        {"chat_id": CHAT, "bot_token": "123:example-token-telegram/../../x"},
        {"chat_id": CHAT, "bot_token": "example-token-telegram"},
        {"chat_id": CHAT},
    ],
)
def test_create_validates_without_echoing(admin: TestClient, config: dict[str, Any]) -> None:
    r = admin.post(f"{API}/channels", json={"type": "telegram", "name": "x", "config": config})
    assert r.status_code == 422
    assert_no_secrets(r.text)


def test_channels_are_admin_only_and_need_csrf(admin: TestClient, session: Session) -> None:
    cid = create(admin)["id"]
    add_user(session, "viewer", Role.VIEWER)
    viewer = TestClient(admin.app, base_url="https://testserver")
    me = login(viewer, "viewer")
    viewer.headers[CSRF_HEADER] = me["csrf_token"]
    assert viewer.get(f"{API}/channels").status_code == 403
    assert viewer.post(f"{API}/channels/{cid}/test").status_code == 403

    csrf = admin.headers.pop(CSRF_HEADER)
    assert admin.delete(f"{API}/channels/{cid}").status_code == 403
    admin.headers[CSRF_HEADER] = csrf


# --- the test message and the sender -----------------------------------------------------


def test_test_message_goes_through_the_guard_to_telegram(
    admin: TestClient, network: FakeNetwork, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    network.serve(ADDRESS, 443, telegram())
    cid = create(admin)["id"]
    r = admin.post(f"{API}/channels/{cid}/test")
    assert r.json() == {"ok": True, "message": "test message sent"}

    assert network.connects == [(ADDRESS, 443)]
    for server_hostname, context in network.tls:
        assert server_hostname == HOST
        assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
    (sent,) = network.requests
    assert sent.method == "POST" and sent.target == f"/bot{TOKEN}/sendMessage"
    payload = json.loads(sent.body)
    assert payload["chat_id"] == CHAT and payload["text"] == ch.TEST_MESSAGE
    assert "parse_mode" not in payload
    # httpx logs the request URL; the filter takes the token out.
    assert "HTTP Request" in caplog.text
    assert_no_secrets(caplog.text)


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        (
            telegram(401, {"ok": False, "description": f"Unauthorized /bot{TOKEN}/x {TOKEN}"}),
            "Telegram API answered HTTP 401: Unauthorized",
        ),
        (telegram(200, {"ok": False}), "Telegram API gave an unexpected answer"),
        (None, "Telegram API unreachable (ConnectError)"),
    ],
)
def test_test_message_failure_never_leaks_the_token(
    admin: TestClient,
    network: FakeNetwork,
    caplog: pytest.LogCaptureFixture,
    answer: bytes | None,
    expected: str,
) -> None:
    caplog.set_level(logging.DEBUG)
    if answer is None:
        network.fail(ADDRESS, 443, httpcore.ConnectError(f"refused for /bot{TOKEN}/sendMessage"))
    else:
        network.serve(ADDRESS, 443, answer)
    cid = create(admin)["id"]
    r = admin.post(f"{API}/channels/{cid}/test")
    assert r.status_code == 200
    assert r.json()["ok"] is False and r.json()["message"].startswith(expected)
    assert_no_secrets(r.text)
    assert_no_secrets(caplog.text)


async def test_telegram_resolving_privately_is_refused(
    network: FakeNetwork, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(DNS, HOST, ["10.0.0.9"])
    config = ch.TelegramConfig(chat_id=CHAT, bot_token=TOKEN)  # type: ignore[arg-type]
    async with factory_for(network)() as client:
        with pytest.raises(ch.SendError) as info:
            await ch.send_telegram(client, config, "hello")
    assert str(info.value).startswith("refused by the network guard")
    assert info.value.__cause__ is None and info.value.__suppress_context__
    assert_no_secrets(str(info.value))
    assert network.connects == []


async def test_only_the_declared_cloud_host_over_verified_https(network: FakeNetwork) -> None:
    network.serve("192.0.2.91", 443, telegram())
    network.serve(ADDRESS, 80, telegram())
    async with factory_for(network)() as client:
        with pytest.raises(net_guard.NetGuardError):
            await client.post("https://api.example.com/sendMessage")
        with pytest.raises(net_guard.NetGuardError):
            await client.post(f"http://{HOST}/sendMessage")
    unverified = net_guard.guarded_http_client(
        NetGuard(
            NetPolicy(own_addresses=frozenset()), cloud_hosts=ch.CLOUD_HOSTS, resolver=fake_resolver
        ),
        verify=False,
        network_backend=network,
    )
    async with unverified as client:
        with pytest.raises(net_guard.NetGuardError):
            await client.post(f"https://{HOST}/sendMessage")
    assert network.connects == []


async def test_default_client_is_guarded_for_telegram_only() -> None:
    async with ch.default_client_factory() as client:
        transport = client._transport
        assert isinstance(transport, net_guard.GuardedTransport)
        assert transport._guard.cloud_hosts == frozenset({HOST})
        assert client.follow_redirects is False


def test_text_is_capped_at_the_telegram_limit() -> None:
    assert ch.cap_text("short") == "short"
    capped = ch.cap_text("x" * 5000)
    assert len(capped) == ch.MAX_TEXT_LENGTH and capped.endswith("…")
    wide = ch.cap_text("\U0001f600" * 3000)  # 2 UTF-16 code units each
    assert len(wide.encode("utf-16-le")) // 2 <= ch.MAX_TEXT_LENGTH


# --- the notifier ------------------------------------------------------------------------


async def test_notifier_sends_to_each_channel_and_never_raises(
    admin: TestClient, engine: Engine, network: FakeNetwork, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    good = create(admin)["id"]
    bad = create(admin, chat_id="-1009999999999")["id"]

    def answer(request: FakeRequest) -> bytes:
        if json.loads(request.body)["chat_id"] == CHAT:
            return telegram()
        return telegram(400, {"ok": False, "description": "Bad Request: chat not found"})

    network.serve(ADDRESS, 443, answer)
    notifier = ch.ChannelNotifier(
        make_session_factory(engine), SecretBox(TEST_MASTER_KEY), factory_for(network)
    )
    await notifier.send([bad, good, 4242], "disk > 85% on nas-1")
    chats = sorted(json.loads(r.body)["chat_id"] for r in network.requests)
    assert chats == sorted([CHAT, "-1009999999999"])
    assert f"alert to channel {bad} failed: Telegram API answered HTTP 400" in caplog.text
    assert_no_secrets(caplog.text)

    def broken() -> httpx.AsyncClient:
        raise RuntimeError(f"boom {TOKEN}")

    await ch.ChannelNotifier(make_session_factory(engine), SecretBox(TEST_MASTER_KEY), broken).send(
        [good], "x"
    )
    assert_no_secrets(caplog.text)


def test_alerts_send_through_the_channel_notifier(engine: Engine, db_url: str) -> None:
    """The alert evaluator's notifier is the real channel sender, not the no-op."""
    from app.alerts.channels import ChannelNotifier

    with make_client(db_url) as c:
        assert isinstance(c.app.state.notifier, ChannelNotifier)  # type: ignore[attr-defined]
