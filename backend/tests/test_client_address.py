"""Client address behind trusted proxies, and the per-client login limit (M1.7)."""

from collections.abc import Iterator
from ipaddress import ip_network

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app.api.auth import LOGIN_LIMITS
from app.api.deps import ClientAddressDep
from app.core.client_address import (
    UNKNOWN_CLIENT,
    TrustedProxiesError,
    parse_trusted_proxies,
    resolve_client_address,
)
from app.core.config import ConfigError, load_settings
from app.core.db import make_session_factory
from app.models import Role
from tests.test_auth import PASSWORD, add_user, login, make_client

PROXY = "192.168.255.242"  # the bundled frontend's fixed address in docker-compose.yml
TRUSTED = (ip_network("192.168.255.242/32"), ip_network("fd00:db8::/64"))
CLIENT_A, CLIENT_B = "192.0.2.10", "192.0.2.11"
CLIENT_LIMIT = LOGIN_LIMITS["client"]


# --- parsing the setting -------------------------------------------------------------


def test_setting_defaults_to_trusting_nobody() -> None:
    assert load_settings({}).trusted_proxies == ()
    assert load_settings({"VANTAGE_TRUSTED_PROXIES": "  "}).trusted_proxies == ()


def test_setting_parses_cidr_list() -> None:
    raw = "192.168.255.242/32, 10.0.0.0/8  fd00:db8::/64,192.168.1.5"
    assert load_settings({"VANTAGE_TRUSTED_PROXIES": raw}).trusted_proxies == (
        ip_network("192.168.255.242/32"),
        ip_network("10.0.0.0/8"),
        ip_network("fd00:db8::/64"),
        ip_network("192.168.1.5/32"),
    )


@pytest.mark.parametrize(
    "raw",
    [
        "frontend",  # hostnames are not accepted
        "10.0.0.5/24",  # host bits set: probably a typo
        "10.0.0.0/33",
        "192.168.255.242/32,not-an-ip",
        "::ffff:192.168.255.242/128",  # would never match: peers are compared as IPv4
    ],
)
def test_invalid_setting_stops_startup(raw: str) -> None:
    with pytest.raises(ConfigError, match="VANTAGE_TRUSTED_PROXIES") as exc:
        load_settings({"VANTAGE_TRUSTED_PROXIES": raw})
    assert raw.split(",")[-1] not in str(exc.value)  # names the variable, not the value


def test_parse_error_is_a_value_error() -> None:
    with pytest.raises(TrustedProxiesError):
        parse_trusted_proxies("nope")


# --- resolving the address -----------------------------------------------------------


def test_untrusted_peer_header_is_ignored() -> None:
    assert resolve_client_address("192.0.2.99", [CLIENT_A], TRUSTED) == "192.0.2.99"


def test_trusted_peer_header_is_used() -> None:
    assert resolve_client_address(PROXY, [CLIENT_A], TRUSTED) == CLIENT_A


def test_nothing_trusted_means_peer_always() -> None:
    assert resolve_client_address(PROXY, [CLIENT_A], ()) == PROXY


@pytest.mark.parametrize(
    "values",
    [
        [],  # missing
        [""],
        ["not-an-ip"],
        ["192.0.2.10, 192.0.2.11"],  # an X-Forwarded-For style list
        ["192.0.2.10:1234"],
        ["0300.0.2.10"],  # leading zeros are ambiguous, refused
        [CLIENT_A, CLIENT_B],  # header sent twice: don't pick one
    ],
)
def test_bad_header_from_trusted_peer_falls_back_to_peer(values: list[str]) -> None:
    assert resolve_client_address(PROXY, values, TRUSTED) == PROXY


def test_ipv4_mapped_peer_is_matched_and_reported_as_ipv4() -> None:
    assert resolve_client_address(f"::ffff:{PROXY}", [CLIENT_A], TRUSTED) == CLIENT_A
    assert resolve_client_address("::ffff:192.0.2.99", [CLIENT_A], TRUSTED) == "192.0.2.99"


def test_ipv4_mapped_header_is_reported_as_ipv4() -> None:
    assert resolve_client_address(PROXY, [f"::FFFF:{CLIENT_A}"], TRUSTED) == CLIENT_A


def test_ipv6_peers() -> None:
    assert resolve_client_address("fd00:db8::7", [CLIENT_A], TRUSTED) == CLIENT_A
    # Untrusted IPv6 peer: its /64, header ignored.
    assert resolve_client_address("2001:DB8:0::1", [CLIENT_A], TRUSTED) == "2001:db8::/64"
    # IPv4 networks never match IPv6 peers and the reverse.
    assert resolve_client_address("fd00:db8::7", [CLIENT_A], TRUSTED[:1]) == "fd00:db8::/64"


def test_trust_check_uses_the_full_ipv6_peer_not_its_64() -> None:
    narrow = (ip_network("fd00:db8::2/128"),)
    assert resolve_client_address("fd00:db8::2", [CLIENT_A], narrow) == CLIENT_A
    assert resolve_client_address("fd00:db8::3", [CLIENT_A], narrow) == "fd00:db8::/64"


def test_ipv6_clients_are_keyed_by_their_64() -> None:
    seen = {resolve_client_address(PROXY, [f"2001:db8:1:2::{i:x}"], TRUSTED) for i in range(1, 50)}
    seen |= {resolve_client_address(f"2001:db8:1:2:ffff::{i:x}", [], ()) for i in range(1, 50)}
    assert seen == {"2001:db8:1:2::/64"}
    assert resolve_client_address(PROXY, ["2001:db8:1:3::1"], TRUSTED) == "2001:db8:1:3::/64"


def test_ipv6_client_in_header_is_canonical_without_zone() -> None:
    assert resolve_client_address(PROXY, ["2001:DB8::0:1"], TRUSTED) == "2001:db8::/64"
    assert resolve_client_address(PROXY, ["fe80::1%eth0"], TRUSTED) == "fe80::/64"


def test_non_ip_or_missing_peer_keeps_old_behaviour() -> None:
    assert resolve_client_address("testclient", [CLIENT_A], TRUSTED) == "testclient"
    assert resolve_client_address(None, [CLIENT_A], TRUSTED) == UNKNOWN_CLIENT


# --- through the app -----------------------------------------------------------------


@pytest.fixture
def db(engine: Engine) -> Iterator[Session]:
    with make_session_factory(engine)() as s:
        yield s


def _client(db_url: str, peer: str, trusted: bool = True) -> TestClient:
    client = make_client(db_url, peer=peer, trusted_proxies=TRUSTED if trusted else ())

    @client.app.get("/api/v1/_test/client-address")  # type: ignore[attr-defined]
    def address(client: ClientAddressDep) -> dict[str, str]:
        return {"address": client}

    return client


def _seen(client: TestClient, headers: dict[str, str] | None = None) -> str:
    address: str = client.get("/api/v1/_test/client-address", headers=headers).json()["address"]
    return address


def _fail_logins(client: TestClient, count: int, real_ip: str | None = None) -> None:
    """Fail `count` logins, each for a different unknown user (so only the client limit hits)."""
    for i in range(count):
        headers = {"X-Real-IP": real_ip} if real_ip else {}
        r = client.post(
            "/api/v1/auth/login",
            json={"username": f"nobody{i}", "password": "wrong"},
            headers=headers,
        )
        assert r.status_code == 401, r.text


def _attempt(client: TestClient, username: str, real_ip: str | None = None) -> int:
    headers = {"X-Real-IP": real_ip} if real_ip else {}
    r = client.post(
        "/api/v1/auth/login", json={"username": username, "password": PASSWORD}, headers=headers
    )
    return r.status_code


def test_app_uses_header_only_from_trusted_peer(db_url: str, engine: Engine) -> None:
    with _client(db_url, PROXY) as proxied:
        assert _seen(proxied, {"X-Real-IP": CLIENT_A}) == CLIENT_A
        assert _seen(proxied, {"X-Real-IP": "garbage"}) == PROXY
        assert _seen(proxied) == PROXY
        # X-Forwarded-For is never read.
        assert _seen(proxied, {"X-Forwarded-For": CLIENT_A}) == PROXY
    with _client(db_url, "192.0.2.99") as direct:
        assert _seen(direct, {"X-Real-IP": CLIENT_A, "X-Forwarded-For": CLIENT_A}) == "192.0.2.99"


def test_app_with_setting_unset_uses_peer(db_url: str, engine: Engine) -> None:
    with _client(db_url, PROXY, trusted=False) as client:
        assert _seen(client, {"X-Real-IP": CLIENT_A}) == PROXY


def test_spoofed_header_from_untrusted_peer_cannot_dodge_the_limit(
    db_url: str, engine: Engine, db: Session
) -> None:
    add_user(db, "viewer1", Role.VIEWER)
    with _client(db_url, "192.0.2.99") as attacker:
        for i in range(CLIENT_LIMIT):
            r = attacker.post(
                "/api/v1/auth/login",
                json={"username": f"nobody{i}", "password": "wrong"},
                headers={"X-Real-IP": f"198.51.100.{i}"},  # a fresh "client" every time
            )
            assert r.status_code == 401
        # Still counted as one client, whatever the header says.
        assert _attempt(attacker, "viewer1", real_ip="203.0.113.1") == 429


def test_one_clients_failures_dont_lock_out_another_behind_the_proxy(
    db_url: str, engine: Engine, db: Session
) -> None:
    add_user(db, "viewer1", Role.VIEWER)
    with _client(db_url, PROXY) as proxy:
        _fail_logins(proxy, CLIENT_LIMIT, real_ip=CLIENT_A)
        assert _attempt(proxy, "viewer1", real_ip=CLIENT_A) == 429
        # Client B, same proxy, is unaffected.
        assert _attempt(proxy, "viewer1", real_ip=CLIENT_B) == 200
        # A is still locked out.
        assert _attempt(proxy, "viewer1", real_ip=CLIENT_A) == 429


def test_username_limit_still_applies_across_clients(
    db_url: str, engine: Engine, db: Session
) -> None:
    add_user(db, "viewer1", Role.VIEWER)
    with _client(db_url, PROXY) as proxy:
        for i in range(LOGIN_LIMITS["user"]):
            r = proxy.post(
                "/api/v1/auth/login",
                json={"username": "viewer1", "password": "wrong"},
                headers={"X-Real-IP": f"198.51.100.{i}"},
            )
            assert r.status_code == 401
        assert _attempt(proxy, "viewer1", real_ip=CLIENT_B) == 429
        login_status = _attempt(proxy, "viewer2-does-not-exist", real_ip=CLIENT_B)
        assert login_status == 401  # other usernames are not blocked for that client


def test_setting_unset_keeps_the_old_global_limit(db_url: str, engine: Engine, db: Session) -> None:
    add_user(db, "viewer1", Role.VIEWER)
    with _client(db_url, PROXY, trusted=False) as proxy:
        _fail_logins(proxy, CLIENT_LIMIT, real_ip=CLIENT_A)
        # Without trusted proxies every request counts against the proxy's address.
        assert _attempt(proxy, "viewer1", real_ip=CLIENT_B) == 429


def test_ipv6_client_rotating_addresses_shares_one_budget(
    db_url: str, engine: Engine, db: Session
) -> None:
    add_user(db, "viewer1", Role.VIEWER)
    with _client(db_url, PROXY) as proxy:
        for i in range(CLIENT_LIMIT):
            r = proxy.post(
                "/api/v1/auth/login",
                json={"username": f"nobody{i}", "password": "wrong"},
                headers={"X-Real-IP": f"2001:db8:1:2::{i + 1:x}"},  # a new address each time
            )
            assert r.status_code == 401
        assert _attempt(proxy, "viewer1", real_ip="2001:db8:1:2::ffff") == 429
        # A different /64 is a different client.
        assert _attempt(proxy, "viewer1", real_ip="2001:db8:1:3::1") == 200


def test_login_works_through_trusted_proxy(db_url: str, engine: Engine, db: Session) -> None:
    add_user(db, "viewer1", Role.VIEWER)
    with _client(db_url, PROXY) as proxy:
        proxy.headers["X-Real-IP"] = CLIENT_A
        assert login(proxy, "viewer1")["username"] == "viewer1"
