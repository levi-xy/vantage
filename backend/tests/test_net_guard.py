"""SSRF guard (Contract 5). No real network: the resolver and the socket layer are fakes.

Only RFC 1918 / RFC 6598 / ULA, RFC 5737 documentation addresses and example.com-style
names appear here. TEST-NET-1 (192.0.2.0/24) stands in for "the public internet" where a
test needs one, by patching the guard's notion of a public address.
"""

from __future__ import annotations

import asyncio
import inspect
import socket
import ssl
from collections.abc import AsyncIterator, Sequence
from ipaddress import IPv4Address, IPv4Network, IPv6Address, ip_address
from typing import Any, ClassVar

import httpcore
import httpx
import pytest
from pydantic import SecretStr
from pysnmp.proto.rfc1902 import Counter32, Integer32, IpAddress, ObjectName, OctetString
from pysnmp.proto.rfc1905 import NoSuchInstance

from app.core import net_guard
from app.core.net_guard import (
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_TOTAL_TIMEOUT,
    MAX_PING_COUNT,
    MAX_PING_TOTAL_TIMEOUT,
    MIN_PING_INTERVAL,
    PING_INTERVAL,
    PING_RESOLVE_TIMEOUT,
    GuardedPingClient,
    GuardedSnmpClient,
    NetGuard,
    NetGuardError,
    NetPolicy,
    PingError,
    SnmpError,
    SnmpV2c,
    SnmpV3,
    check_address,
    guarded_http_client,
    parse_cidrs,
    parse_host,
)
from tests.integrations.netfake import FakeIcmp, always, drop

FAKE_COMMUNITY = "example-community"

# Kept before the autouse fixture below patches the module attribute.
real_discover_own_addresses = net_guard.discover_own_addresses

# --- fakes -------------------------------------------------------------------------


class FakeResolver:
    """Returns a scripted answer per call, so successive lookups can disagree."""

    def __init__(self, *answers: Sequence[str]) -> None:
        self._answers = list(answers)
        self.calls: list[tuple[str, int]] = []

    async def __call__(self, host: str, port: int) -> Sequence[str]:
        self.calls.append((host, port))
        if len(self._answers) > 1:
            return self._answers.pop(0)
        return self._answers[0]


async def no_resolver(host: str, port: int) -> Sequence[str]:
    raise AssertionError(f"resolver must not be called for {host!r}")


def response_bytes(status: str = "200 OK", headers: str = "", body: bytes = b"ok") -> bytes:
    return f"HTTP/1.1 {status}\r\n{headers}Content-Length: {len(body)}\r\n\r\n".encode() + body


class RecordingStream(httpcore.AsyncMockStream):
    def __init__(self, buffer: list[bytes], backend: RecordingBackend) -> None:
        super().__init__(buffer)
        self._backend = backend

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:  # noqa: ASYNC109
        self._backend.written += buffer

    async def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,  # noqa: ASYNC109
    ) -> httpcore.AsyncNetworkStream:
        self._backend.tls.append((server_hostname, ssl_context))
        return self


class RecordingBackend(httpcore.AsyncNetworkBackend):
    """Stands in for the socket layer; records where the guard told it to connect."""

    def __init__(self, *responses: bytes) -> None:
        self._responses = list(responses) or [response_bytes()]
        self.connects: list[tuple[str, int, float | None]] = []
        self.tls: list[tuple[str | None, ssl.SSLContext]] = []
        self.written = b""

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        self.connects.append((host, port, timeout))
        response = self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]
        return RecordingStream([response], self)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


@pytest.fixture(autouse=True)
def fixed_own_addresses(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make NetPolicy()'s default discovery deterministic: the test machine's real
    addresses must not decide whether a private test address is allowed."""
    monkeypatch.setattr(
        net_guard, "discover_own_addresses", lambda: frozenset({IPv4Address("10.255.0.1")})
    )


@pytest.fixture
def public_test_net(monkeypatch: pytest.MonkeyPatch) -> None:
    """Treat TEST-NET-1 as public internet for cloud-host tests."""
    test_net = IPv4Network("192.0.2.0/24")
    original = net_guard._is_public
    monkeypatch.setattr(net_guard, "_is_public", lambda addr: addr in test_net or original(addr))


# --- address policy ----------------------------------------------------------------

DENIED = [
    "0.0.0.0",  # noqa: S104 - a denied target, not a bind address
    "0.1.2.3",
    "127.0.0.1",
    "127.255.255.254",
    "169.254.0.1",
    "169.254.169.254",  # cloud metadata
    "100.100.100.200",  # Alibaba metadata, inside CGNAT
    "224.0.0.1",
    "239.255.255.250",
    "240.0.0.1",
    "255.255.255.255",
    "::",
    "::1",
    "fe80::1",
    "febf::1",
    "fec0::1",
    "ff02::1",
    "ff0e::1",
    "fd00:ec2::254",  # AWS metadata over IPv6, inside ULA
    # IPv4-mapped / embedded IPv4 forms of denied addresses
    "::ffff:127.0.0.1",
    "::ffff:169.254.169.254",
    "::ffff:0.0.0.0",
    "::ffff:224.0.0.1",
    "::127.0.0.1",  # deprecated IPv4-compatible
    "64:ff9b::7f00:1",  # NAT64 of 127.0.0.1
    "64:ff9b::a9fe:a9fe",  # NAT64 of 169.254.169.254
    "64:ff9b:1::7f00:1",  # RFC 8215 local-use NAT64 of 127.0.0.1
    "64:ff9b:1::a9fe:a9fe",  # RFC 8215 local-use NAT64 of 169.254.169.254
    "2002:7f00:1::1",  # 6to4 of 127.0.0.1
    "2002:a9fe:a9fe::1",  # 6to4 of 169.254.169.254
    # outside every allowed range
    "192.0.2.10",
    "198.51.100.1",
    "203.0.113.1",
    "2001:db8::1",
]

ALLOWED = [
    ("10.0.0.1", "10.0.0.1"),
    ("10.255.255.254", "10.255.255.254"),
    ("172.16.0.1", "172.16.0.1"),
    ("172.31.255.254", "172.31.255.254"),
    ("192.168.0.1", "192.168.0.1"),
    ("192.168.255.254", "192.168.255.254"),
    ("100.64.0.1", "100.64.0.1"),
    ("100.127.255.254", "100.127.255.254"),
    ("fc00::1", "fc00::1"),
    ("fd12:3456:789a::1", "fd12:3456:789a::1"),
    ("::ffff:10.0.0.1", "10.0.0.1"),  # mapped form connects to the IPv4 address
]


@pytest.mark.parametrize("address", DENIED)
@pytest.mark.parametrize("cloud_host", [False, True])
def test_denied_addresses(address: str, cloud_host: bool) -> None:
    with pytest.raises(NetGuardError):
        check_address(ip_address(address), NetPolicy(), cloud_host=cloud_host)


@pytest.mark.parametrize(("address", "expected"), ALLOWED)
def test_allowed_addresses(address: str, expected: str) -> None:
    assert check_address(ip_address(address), NetPolicy(), cloud_host=False) == ip_address(expected)


def test_extra_allowed_cidrs() -> None:
    policy = NetPolicy(extra_allowed=parse_cidrs(["192.0.2.0/24", "2001:db8:1::/48"]))
    assert check_address(ip_address("192.0.2.7"), policy, cloud_host=False)
    assert check_address(ip_address("2001:db8:1::7"), policy, cloud_host=False)
    with pytest.raises(NetGuardError):
        check_address(ip_address("198.51.100.7"), policy, cloud_host=False)


def test_local_use_nat64_embedded_ipv4_judged_like_nat64() -> None:
    # Even when an admin allows the local-use NAT64 prefix, embedded denied IPv4 wins.
    policy = NetPolicy(extra_allowed=parse_cidrs(["64:ff9b:1::/48"]))
    for address in ("64:ff9b:1::7f00:1", "64:ff9b:1::a9fe:a9fe"):
        with pytest.raises(NetGuardError):
            check_address(ip_address(address), policy, cloud_host=False)
    assert check_address(ip_address("64:ff9b:1::a00:1"), policy, cloud_host=False)


@pytest.mark.parametrize(
    "cidr", ["127.0.0.0/8", "169.254.0.0/16", "0.0.0.0/0", "::/0", "fd00:ec2::/32"]
)
def test_extra_allowed_cidrs_never_override_denied(cidr: str) -> None:
    policy = NetPolicy(extra_allowed=parse_cidrs([cidr]))
    for address in ("127.0.0.1", "169.254.169.254", "::1", "fd00:ec2::254", "::ffff:127.0.0.1"):
        with pytest.raises(NetGuardError):
            check_address(ip_address(address), policy, cloud_host=True)


@pytest.mark.parametrize("bad", ["10.0.0.1/8", "not-a-cidr", "10.0.0.0/33", ""])
def test_parse_cidrs_rejects_invalid(bad: str) -> None:
    with pytest.raises(ValueError):
        parse_cidrs([bad])


def test_own_addresses_denied() -> None:
    policy = NetPolicy(own_addresses=frozenset({IPv4Address("10.0.0.5"), IPv6Address("fd00::5")}))
    for address in ("10.0.0.5", "::ffff:10.0.0.5", "fd00::5"):
        with pytest.raises(NetGuardError):
            check_address(ip_address(address), policy, cloud_host=False)
    assert check_address(ip_address("10.0.0.6"), policy, cloud_host=False)


def test_discover_own_addresses(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_getaddrinfo(*args: Any, **kwargs: Any) -> list[Any]:
        return [(2, 1, 6, "", ("10.0.0.42", 0)), (10, 1, 6, "", ("::ffff:10.0.0.43", 0, 0, 0))]

    monkeypatch.setattr(net_guard.socket, "getaddrinfo", fake_getaddrinfo)
    found = real_discover_own_addresses()
    assert IPv4Address("10.0.0.42") in found
    assert IPv4Address("10.0.0.43") in found  # stored unwrapped
    assert all(not (isinstance(a, IPv6Address) and a.ipv4_mapped) for a in found)


async def test_default_policy_discovers_own_addresses() -> None:
    """Fail safe: a caller who never mentions own addresses still gets them denied."""
    assert IPv4Address("10.255.0.1") in NetPolicy().own_addresses
    with pytest.raises(NetGuardError):
        check_address(ip_address("10.255.0.1"), NetPolicy(), cloud_host=False)
    with pytest.raises(NetGuardError):
        await NetGuard(resolver=no_resolver).resolve("10.255.0.1", 80)
    with pytest.raises(NetGuardError):
        await NetGuard(resolver=FakeResolver(["10.255.0.1"])).resolve("nas.example.com", 80)
    # An explicit set is used as given.
    assert NetPolicy(own_addresses=frozenset()).own_addresses == frozenset()


def test_cloud_host_requires_public_address(public_test_net: None) -> None:
    for address in ("10.0.0.7", "172.16.0.7", "192.168.0.7", "100.64.0.7", "fc00::7"):
        with pytest.raises(NetGuardError):
            check_address(ip_address(address), NetPolicy(), cloud_host=True)
    for address in ("64:ff9b::a00:1", "2002:a00:1::", "64:ff9b:1::a00:1", "192.0.0.9"):
        with pytest.raises(NetGuardError):
            check_address(ip_address(address), NetPolicy(), cloud_host=True)
    # Extra allowed CIDRs don't make a non-public answer acceptable for a cloud host.
    policy = NetPolicy(extra_allowed=parse_cidrs(["198.51.100.0/24"]))
    with pytest.raises(NetGuardError):
        check_address(ip_address("198.51.100.7"), policy, cloud_host=True)


def test_public_needs_cloud_host(public_test_net: None) -> None:
    assert check_address(ip_address("192.0.2.10"), NetPolicy(), cloud_host=True)
    with pytest.raises(NetGuardError):
        check_address(ip_address("192.0.2.10"), NetPolicy(), cloud_host=False)


# --- host parsing ------------------------------------------------------------------

AMBIGUOUS_OR_INVALID = [
    "2130706433",  # decimal 127.0.0.1
    "0x7f000001",  # hex
    "0x7f.0.0.1",
    "0x7f.1",
    "0177.0.0.1",  # octal
    "017700000001",
    "127.1",  # short form
    "10.1",
    "010.0.0.1",  # leading zero: octal to inet_aton
    "0x0a.0.0.1",
    "1.2.3.4.5",
    "example.com.0x10",
    "",
    "exa mple.com",
    "exämple.com",
    "nas.example.com/path",
    "user@nas.example.com",
    "nas.example.com:8080",
    "-bad.example.com",
    "fe80::1%eth0",
    "[fe80::1%25eth0]",
    "a" * 254,
]


@pytest.mark.parametrize("host", AMBIGUOUS_OR_INVALID)
async def test_ambiguous_or_invalid_hosts_rejected_without_resolving(host: str) -> None:
    with pytest.raises(NetGuardError):
        parse_host(host)
    with pytest.raises(NetGuardError):
        await NetGuard(resolver=no_resolver).resolve(host, 80)


def test_parse_host_normalizes() -> None:
    assert parse_host("NAS.Example.COM.") == "nas.example.com"
    assert parse_host("[fd00::1]") == IPv6Address("fd00::1")
    assert parse_host("10.0.0.1") == IPv4Address("10.0.0.1")
    assert parse_host("svc_1.example.internal") == "svc_1.example.internal"


# --- resolve once, check every answer ------------------------------------------------


async def test_resolves_and_returns_checked_address() -> None:
    resolver = FakeResolver(["10.0.0.7", "10.0.0.8"])
    guard = NetGuard(resolver=resolver)
    assert await guard.resolve("nas.example.com", 443) == IPv4Address("10.0.0.7")
    assert resolver.calls == [("nas.example.com", 443)]


@pytest.mark.parametrize(
    "answers",
    [
        ["10.0.0.7", "127.0.0.1"],
        ["127.0.0.1", "10.0.0.7"],
        ["10.0.0.7", "169.254.169.254"],
        ["fd00::7", "::1"],
        ["10.0.0.7", "::ffff:127.0.0.1"],
        ["10.0.0.7", "192.0.2.1"],
        ["10.0.0.7", "fe80::1%eth0"],
    ],
)
async def test_mixed_answers_are_denied(answers: list[str]) -> None:
    with pytest.raises(NetGuardError):
        await NetGuard(resolver=FakeResolver(answers)).resolve("nas.example.com", 80)


async def test_literal_address_skips_resolver() -> None:
    guard = NetGuard(resolver=no_resolver)
    assert await guard.resolve("10.0.0.9", 80) == IPv4Address("10.0.0.9")
    with pytest.raises(NetGuardError):
        await guard.resolve("127.0.0.1", 80)


async def test_resolution_failures() -> None:
    async def failing(host: str, port: int) -> Sequence[str]:
        raise OSError("no such host")

    with pytest.raises(NetGuardError):
        await NetGuard(resolver=failing).resolve("nas.example.com", 80)
    with pytest.raises(NetGuardError):
        await NetGuard(resolver=FakeResolver([])).resolve("nas.example.com", 80)
    with pytest.raises(NetGuardError):
        await NetGuard(resolver=FakeResolver(["garbage"])).resolve("nas.example.com", 80)


@pytest.mark.parametrize("port", [0, -1, 65536])
async def test_invalid_port(port: int) -> None:
    with pytest.raises(NetGuardError):
        await NetGuard(resolver=no_resolver).resolve("10.0.0.1", port)


def test_cloud_hosts_must_be_names() -> None:
    with pytest.raises(ValueError):
        NetGuard(cloud_hosts=["192.0.2.1"])
    assert NetGuard(cloud_hosts=["API.Example.com"]).is_cloud_host("api.example.com.")


# --- HTTP client -----------------------------------------------------------------------


async def test_http_connects_to_checked_address_with_original_host_header() -> None:
    resolver = FakeResolver(["10.0.0.7"])
    backend = RecordingBackend()
    async with guarded_http_client(NetGuard(resolver=resolver), network_backend=backend) as c:
        response = await c.get("http://nas.example.com:8080/api/status")
    assert response.status_code == 200
    assert backend.connects[0][:2] == ("10.0.0.7", 8080)
    assert b"Host: nas.example.com:8080\r\n" in backend.written
    assert resolver.calls == [("nas.example.com", 8080)]


IDN_HOST = "xn--e1afmkfd.example"  # punycode label, as the user would configure it


@pytest.mark.parametrize(
    "url", [f"http://{IDN_HOST}/", "http://\u043f\u0440\u0438\u043c\u0435\u0440.example/"]
)
async def test_idn_host_allowed_private(url: str) -> None:
    resolver = FakeResolver(["10.0.0.30"])
    backend = RecordingBackend()
    async with guarded_http_client(NetGuard(resolver=resolver), network_backend=backend) as c:
        response = await c.get(url)
    assert response.status_code == 200
    assert resolver.calls == [(IDN_HOST, 80)]
    assert backend.connects[0][:2] == ("10.0.0.30", 80)
    assert f"Host: {IDN_HOST}\r\n".encode() in backend.written


async def test_idn_host_resolving_to_denied_address() -> None:
    backend = RecordingBackend()
    guard = NetGuard(resolver=FakeResolver(["127.0.0.1"]))
    async with guarded_http_client(guard, network_backend=backend) as c:
        with pytest.raises(NetGuardError):
            await c.get(f"http://{IDN_HOST}/")
    assert backend.connects == []


async def test_https_keeps_sni_and_verification() -> None:
    backend = RecordingBackend()
    guard = NetGuard(resolver=FakeResolver(["10.0.0.7"]))
    async with guarded_http_client(guard, network_backend=backend) as c:
        await c.get("https://nas.example.com/")
    assert backend.connects[0][:2] == ("10.0.0.7", 443)
    server_hostname, context = backend.tls[0]
    assert server_hostname == "nas.example.com"
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


async def test_dns_rebinding_cannot_change_connected_address() -> None:
    # First answer is safe, every later answer points at loopback.
    resolver = FakeResolver(["10.0.0.7"], ["127.0.0.1"])
    backend = RecordingBackend()
    guard = NetGuard(resolver=resolver)
    async with guarded_http_client(guard, network_backend=backend) as c:
        await c.get("http://rebind.example.com/")
    # Resolved exactly once, and the socket went to the address that was checked.
    assert len(resolver.calls) == 1
    assert [host for host, _, _ in backend.connects] == ["10.0.0.7"]

    # A new connection resolves again and the rebound answer is refused.
    async with guarded_http_client(guard, network_backend=backend) as c:
        with pytest.raises(NetGuardError):
            await c.get("http://rebind.example.com/")
    assert len(backend.connects) == 1


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://[::ffff:a9fe:a9fe]/",
        "http://[fd00:ec2::254]/",
        "http://[64:ff9b::7f00:1]/",
        "http://2130706433/",
        "http://0x7f.1/",
        "http://0177.0.0.1/",
        "http://127.1/",
        "http://192.0.2.1/",
    ],
)
async def test_http_denied_targets_never_connect(url: str) -> None:
    backend = RecordingBackend()
    async with guarded_http_client(NetGuard(resolver=no_resolver), network_backend=backend) as c:
        # httpx itself already rejects some forms (e.g. octal dotted quads) as invalid URLs.
        with pytest.raises((NetGuardError, httpx.InvalidURL)):
            await c.get(url)
    assert backend.connects == []


async def test_http_hostname_resolving_to_denied_address() -> None:
    backend = RecordingBackend()
    guard = NetGuard(resolver=FakeResolver(["169.254.169.254"]))
    async with guarded_http_client(guard, network_backend=backend) as c:
        with pytest.raises(NetGuardError):
            await c.get("http://metadata.example.com/")
    assert backend.connects == []


@pytest.mark.parametrize("url", ["ftp://10.0.0.1/", "file:///etc/hosts", "ws://10.0.0.1/"])
async def test_http_only_http_and_https(url: str) -> None:
    backend = RecordingBackend()
    async with guarded_http_client(NetGuard(resolver=no_resolver), network_backend=backend) as c:
        with pytest.raises((NetGuardError, httpx.UnsupportedProtocol)):
            await c.get(url)
    assert backend.connects == []


async def test_redirects_not_followed() -> None:
    backend = RecordingBackend(response_bytes("302 Found", "Location: http://127.0.0.1/\r\n", b""))
    async with guarded_http_client(NetGuard(resolver=no_resolver), network_backend=backend) as c:
        response = await c.get("http://10.0.0.7/")
    assert response.status_code == 302
    assert len(backend.connects) == 1


async def test_redirect_hops_are_rechecked_when_caller_opts_in() -> None:
    backend = RecordingBackend(response_bytes("302 Found", "Location: http://127.0.0.1/\r\n", b""))
    async with guarded_http_client(NetGuard(resolver=no_resolver), network_backend=backend) as c:
        with pytest.raises(NetGuardError):
            await c.get("http://10.0.0.7/", follow_redirects=True)
    assert [host for host, _, _ in backend.connects] == ["10.0.0.7"]

    resolver = FakeResolver(["10.0.0.8"])
    backend = RecordingBackend(
        response_bytes("302 Found", "Location: http://other.example.com/\r\n", b""),
        response_bytes(),
    )
    async with guarded_http_client(NetGuard(resolver=resolver), network_backend=backend) as c:
        response = await c.get("http://10.0.0.7/", follow_redirects=True)
    assert response.status_code == 200
    assert resolver.calls == [("other.example.com", 80)]
    assert [host for host, _, _ in backend.connects] == ["10.0.0.7", "10.0.0.8"]


async def test_default_timeouts() -> None:
    backend = RecordingBackend()
    client = guarded_http_client(NetGuard(resolver=no_resolver), network_backend=backend)
    async with client as c:
        assert c.timeout == httpx.Timeout(DEFAULT_TOTAL_TIMEOUT, connect=DEFAULT_CONNECT_TIMEOUT)
        assert c.follow_redirects is False
        assert c.trust_env is False
        await c.get("http://10.0.0.7/")
        # Callers cannot disable or lengthen the timeouts.
        await c.get("http://10.0.0.8/", timeout=None)
        await c.get("http://10.0.0.9/", timeout=600)
    assert DEFAULT_CONNECT_TIMEOUT == 5.0
    assert DEFAULT_TOTAL_TIMEOUT == 15.0
    assert [t for _, _, t in backend.connects] == [5.0, 5.0, 5.0]


class HangingStream(httpcore.AsyncMockStream):
    def __init__(self, first: list[bytes]) -> None:
        super().__init__(first)

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:  # noqa: ASYNC109
        if self._buffer:
            return self._buffer.pop(0)
        await asyncio.sleep(3600)
        return b""


class HangingBackend(RecordingBackend):
    def __init__(self, first: list[bytes]) -> None:
        super().__init__()
        self._first = first

    async def connect_tcp(self, host: str, port: int, *args: Any, **kwargs: Any) -> Any:
        self.connects.append((host, port, kwargs.get("timeout")))
        return HangingStream(list(self._first))


async def test_total_timeout_while_waiting_for_headers() -> None:
    backend = HangingBackend([])
    client = guarded_http_client(
        NetGuard(resolver=no_resolver), network_backend=backend, total_timeout=0.2
    )
    async with client as c:
        with pytest.raises(httpx.TimeoutException):
            await asyncio.wait_for(c.get("http://10.0.0.7/"), 5)


async def test_total_timeout_while_reading_body() -> None:
    backend = HangingBackend([b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\npartial"])
    client = guarded_http_client(
        NetGuard(resolver=no_resolver), network_backend=backend, total_timeout=0.2
    )
    async with client as c:
        with pytest.raises(httpx.TimeoutException):
            await asyncio.wait_for(c.get("http://10.0.0.7/"), 5)


async def test_slow_resolver_hits_connect_timeout() -> None:
    async def slow(host: str, port: int) -> Sequence[str]:
        await asyncio.sleep(3600)
        return ["10.0.0.7"]

    backend = RecordingBackend()
    client = guarded_http_client(
        NetGuard(resolver=slow), network_backend=backend, connect_timeout=0.1
    )
    async with client as c:
        with pytest.raises(httpx.ConnectTimeout):
            await asyncio.wait_for(c.get("http://slow.example.com/"), 5)
    assert backend.connects == []


async def test_environment_proxy_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "all_proxy"):
        monkeypatch.setenv(var, "http://10.0.0.99:3128")
    backend = RecordingBackend()
    async with guarded_http_client(NetGuard(resolver=no_resolver), network_backend=backend) as c:
        await c.get("http://10.0.0.7/")
    assert backend.connects[0][:2] == ("10.0.0.7", 80)


async def test_unix_sockets_refused() -> None:
    backend = net_guard._GuardedNetworkBackend(NetGuard(resolver=no_resolver))
    with pytest.raises(NetGuardError):
        await backend.connect_unix_socket("/var/run/docker.sock")


# --- cloud hosts ---------------------------------------------------------------------


async def test_cloud_host_over_https_reaches_public_address(public_test_net: None) -> None:
    backend = RecordingBackend()
    guard = NetGuard(cloud_hosts=["api.example.com"], resolver=FakeResolver(["192.0.2.50"]))
    async with guarded_http_client(guard, network_backend=backend) as c:
        response = await c.get("https://api.example.com/v1/status")
    assert response.status_code == 200
    assert backend.connects[0][:2] == ("192.0.2.50", 443)
    server_hostname, context = backend.tls[0]
    assert server_hostname == "api.example.com"
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname


async def test_cloud_host_public_only_answers_allowed(public_test_net: None) -> None:
    resolver = FakeResolver(["192.0.2.50", "192.0.2.51"])
    guard = NetGuard(cloud_hosts=["api.example.com"], resolver=resolver)
    assert await guard.resolve("api.example.com", 443, cloud_host=True) == IPv4Address("192.0.2.50")


async def test_cloud_host_plain_http_refused(public_test_net: None) -> None:
    backend = RecordingBackend()
    guard = NetGuard(cloud_hosts=["api.example.com"], resolver=no_resolver)
    async with guarded_http_client(guard, network_backend=backend) as c:
        with pytest.raises(NetGuardError):
            await c.get("http://api.example.com/")
    assert backend.connects == []


async def test_cloud_host_without_verification_refused(public_test_net: None) -> None:
    backend = RecordingBackend()
    guard = NetGuard(cloud_hosts=["api.example.com"], resolver=no_resolver)
    unverified = ssl.create_default_context()
    unverified.check_hostname = False
    for verify in (False, unverified):
        client = guarded_http_client(guard, verify=verify, network_backend=backend)
        async with client as c:
            with pytest.raises(NetGuardError):
                await c.get("https://api.example.com/")
    assert backend.connects == []


async def test_undeclared_host_cannot_reach_public(public_test_net: None) -> None:
    backend = RecordingBackend()
    guard = NetGuard(cloud_hosts=["api.example.com"], resolver=FakeResolver(["192.0.2.50"]))
    async with guarded_http_client(guard, network_backend=backend) as c:
        with pytest.raises(NetGuardError):
            await c.get("https://other.example.com/")
        with pytest.raises(NetGuardError):
            await c.get("https://192.0.2.50/")
    assert backend.connects == []


@pytest.mark.parametrize(
    "answers",
    [
        ["169.254.169.254"],
        ["192.0.2.50", "127.0.0.1"],
        ["::ffff:127.0.0.1"],
        # Cloud hosts must resolve to public addresses only.
        ["10.0.0.7"],
        ["100.64.0.7"],
        ["fc00::7"],
        ["fd12:3456::7"],
        ["::ffff:10.0.0.7"],
        ["192.0.2.50", "10.0.0.7"],
        ["10.0.0.7", "192.0.2.50"],
        ["192.0.2.50", "100.64.0.7"],
        ["192.0.2.50", "fc00::7"],
        # IPv6 forms embedding a private IPv4 address are not "public".
        ["64:ff9b::a00:1"],  # NAT64 of 10.0.0.1
        ["2002:a00:1::"],  # 6to4 of 10.0.0.1
        ["64:ff9b:1::a00:1"],  # RFC 8215 local-use NAT64 of 10.0.0.1
        ["192.0.2.50", "64:ff9b::a00:1"],
        ["64:ff9b::a00:1", "192.0.2.50"],
        ["192.0.2.50", "2002:a00:1::"],
        ["2002:a00:1::", "192.0.2.50"],
        ["192.0.2.50", "64:ff9b:1::a00:1"],
        ["64:ff9b:1::a00:1", "192.0.2.50"],
        ["192.0.0.9"],  # 192.0.0.0/24 has is_global entries
        ["192.0.2.50", "192.0.0.10"],
        ["198.51.100.7"],  # non-global (documentation) and not our stand-in public range
    ],
)
async def test_cloud_host_still_denied_internal(public_test_net: None, answers: list[str]) -> None:
    backend = RecordingBackend()
    guard = NetGuard(cloud_hosts=["api.example.com"], resolver=FakeResolver(answers))
    async with guarded_http_client(guard, network_backend=backend) as c:
        with pytest.raises(NetGuardError):
            await c.get("https://api.example.com/")
    assert backend.connects == []


# --- SNMP ------------------------------------------------------------------------------


class SnmpRecorder:
    def __init__(self, result: Any = None, error: Any = None) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.kwargs: list[dict[str, Any]] = []
        self.result = result or []
        self.error = error

    async def get_cmd(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append(args)
        self.kwargs.append(kwargs)
        return self.error, 0, 0, self.result

    async def walk_cmd(self, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        self.calls.append(args)
        self.kwargs.append(kwargs)
        for row in self.result:
            yield None, 0, 0, (row,)


@pytest.fixture
def snmp(monkeypatch: pytest.MonkeyPatch) -> SnmpRecorder:
    recorder = SnmpRecorder()
    monkeypatch.setattr(net_guard, "get_cmd", recorder.get_cmd)
    monkeypatch.setattr(net_guard, "walk_cmd", recorder.walk_cmd)
    return recorder


def v2c() -> SnmpV2c:
    return SnmpV2c(community=SecretStr(FAKE_COMMUNITY))


@pytest.mark.parametrize(
    "host", ["127.0.0.1", "169.254.169.254", "::1", "::ffff:127.0.0.1", "192.0.2.1", "2130706433"]
)
async def test_snmp_denied_hosts(snmp: SnmpRecorder, host: str) -> None:
    client = GuardedSnmpClient(NetGuard(resolver=no_resolver), host, v2c())
    with pytest.raises(NetGuardError):
        await client.get(["1.3.6.1.2.1.1.1.0"])
    with pytest.raises(NetGuardError):
        await client.walk("1.3.6.1.2.1.2.2")
    assert snmp.calls == []


async def test_snmp_uses_checked_address_and_converts_values(snmp: SnmpRecorder) -> None:
    snmp.result = [
        (ObjectName("1.3.6.1.2.1.1.1.0"), OctetString(b"example switch")),
        (ObjectName("1.3.6.1.2.1.2.2.1.10.1"), Counter32(12345)),
        (ObjectName("1.3.6.1.2.1.4.20.1.1.10.0.0.20"), IpAddress("10.0.0.20")),
        (ObjectName("1.3.6.1.2.1.1.7.0"), Integer32(72)),
        (ObjectName("1.3.6.1.2.1.1.2.0"), ObjectName("1.3.6.1.4.1.8072.3.2.10")),
        (ObjectName("1.3.6.1.2.1.99.0"), NoSuchInstance()),
    ]
    resolver = FakeResolver(["10.0.0.20"], ["127.0.0.1"])  # rebinding attempt
    client = GuardedSnmpClient(NetGuard(resolver=resolver), "switch.example.com", v2c())
    try:
        rows = await client.get(["1.3.6.1.2.1.1.1.0", ".1.3.6.1.2.1.2.2.1.10.1"])
    finally:
        client.close()
    target = snmp.calls[0][2]
    assert target.transport_address == ("10.0.0.20", 161)
    assert target.timeout == DEFAULT_CONNECT_TIMEOUT
    assert resolver.calls == [("switch.example.com", 161)]
    assert snmp.kwargs[0] == {"lookupMib": False}
    assert [r.value for r in rows] == [
        b"example switch",
        12345,
        "10.0.0.20",
        72,
        "1.3.6.1.4.1.8072.3.2.10",
        None,
    ]
    assert rows[0].oid == "1.3.6.1.2.1.1.1.0"


async def test_snmp_ipv6_target(snmp: SnmpRecorder) -> None:
    client = GuardedSnmpClient(NetGuard(resolver=no_resolver), "fd00::20", v2c(), port=1161)
    try:
        await client.get(["1.3.6.1.2.1.1.1.0"])
    finally:
        client.close()
    target = snmp.calls[0][2]
    assert target.transport_address == ("fd00::20", 1161, 0, 0)


async def test_snmp_never_uses_cloud_exemption(snmp: SnmpRecorder, public_test_net: None) -> None:
    guard = NetGuard(cloud_hosts=["api.example.com"], resolver=FakeResolver(["192.0.2.50"]))
    client = GuardedSnmpClient(guard, "api.example.com", v2c())
    with pytest.raises(NetGuardError):
        await client.get(["1.3.6.1.2.1.1.1.0"])
    assert snmp.calls == []


async def test_snmp_walk_stays_in_subtree(snmp: SnmpRecorder) -> None:
    snmp.result = [
        (ObjectName("1.3.6.1.2.1.2.2.1.2.1"), OctetString(b"eth0")),
        (ObjectName("1.3.6.1.2.1.2.2.1.2.2"), OctetString(b"eth1")),
    ]
    client = GuardedSnmpClient(NetGuard(resolver=no_resolver), "10.0.0.20", v2c())
    try:
        rows = await client.walk("1.3.6.1.2.1.2.2.1.2", max_rows=50)
    finally:
        client.close()
    assert [r.value for r in rows] == [b"eth0", b"eth1"]
    assert snmp.kwargs[0] == {"lookupMib": False, "lexicographicMode": False, "maxRows": 50}


@pytest.mark.parametrize("oid", ["SNMPv2-MIB::sysDescr.0", "1.3.6.1.", "1..3", "iso.3.6"])
async def test_snmp_rejects_symbolic_or_bad_oids(snmp: SnmpRecorder, oid: str) -> None:
    client = GuardedSnmpClient(NetGuard(resolver=no_resolver), "10.0.0.20", v2c())
    with pytest.raises(ValueError):
        await client.get([oid])
    assert snmp.calls == []


async def test_snmp_errors_do_not_leak_community(snmp: SnmpRecorder) -> None:
    snmp.error = "No SNMP response received before timeout"
    client = GuardedSnmpClient(NetGuard(resolver=no_resolver), "10.0.0.20", v2c())
    try:
        with pytest.raises(SnmpError) as info:
            await client.get(["1.3.6.1.2.1.1.1.0"])
    finally:
        client.close()
    assert FAKE_COMMUNITY not in str(info.value)
    assert FAKE_COMMUNITY not in repr(client.__dict__)


async def test_snmp_total_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    async def hanging(*args: Any, **kwargs: Any) -> Any:
        await asyncio.sleep(3600)

    monkeypatch.setattr(net_guard, "get_cmd", hanging)
    client = GuardedSnmpClient(
        NetGuard(resolver=no_resolver), "10.0.0.20", v2c(), total_timeout=0.1
    )
    try:
        with pytest.raises(SnmpError):
            await asyncio.wait_for(client.get(["1.3.6.1.2.1.1.1.0"]), 5)
    finally:
        client.close()


def test_snmp_v3_auth() -> None:
    guard = NetGuard(resolver=no_resolver)
    auth = SnmpV3(
        username="monitor",
        auth_key=SecretStr("example-auth-key"),
        priv_key=SecretStr("example-priv-key"),
        auth_protocol="sha256",
        priv_protocol="aes256",
    )
    data = GuardedSnmpClient(guard, "10.0.0.20", auth)._auth_data()
    assert data.userName == "monitor"
    bad = SnmpV3(username="monitor", priv_key=SecretStr("example-priv-key"))
    with pytest.raises(ValueError):
        GuardedSnmpClient(guard, "10.0.0.20", bad)._auth_data()


def test_snmp_is_read_only() -> None:
    """Hard rule 2: no SET (or any write) operation is reachable through the guard."""
    source = inspect.getsource(net_guard)
    for write_op in ("set_cmd", "setCmd", "SetRequest"):
        assert write_op not in source
    public = {name for name in dir(GuardedSnmpClient) if not name.startswith("_")}
    assert public == {"get", "walk", "close"}


# --- ICMP ping ---------------------------------------------------------------------------
#
# The guard is real; only the socket layer is fake (tests.integrations.netfake.FakeIcmp),
# injected as the client's socket_factory.

PING_OK = "10.0.0.30"
# The ping client's tests run on the default asyncio loop and on uvloop (what uvicorn
# runs in the container); see pytest_asyncio_loop_factories in tests/conftest.py.
BOTH_LOOPS = pytest.mark.asyncio(loop_factories=["asyncio", "uvloop"])


def pinger(
    host: str,
    icmp: FakeIcmp,
    *,
    guard: NetGuard | None = None,
    count: int = 1,
    interval: float = MIN_PING_INTERVAL,
    reply_timeout: float = 0.5,
    total_timeout: float = MAX_PING_TOTAL_TIMEOUT,
) -> GuardedPingClient:
    return GuardedPingClient(
        guard or NetGuard(resolver=no_resolver),
        host,
        count=count,
        interval=interval,
        reply_timeout=reply_timeout,
        total_timeout=total_timeout,
        socket_factory=icmp.open,
    )


@pytest.fixture
def icmp() -> FakeIcmp:
    return FakeIcmp()


@pytest.mark.parametrize("address", DENIED)
@BOTH_LOOPS
async def test_ping_denied_addresses_never_open_a_socket(icmp: FakeIcmp, address: str) -> None:
    with pytest.raises(NetGuardError):
        await pinger(address, icmp).ping()
    assert icmp.opened == []
    assert icmp.sent == []


@pytest.mark.parametrize("host", AMBIGUOUS_OR_INVALID)
@BOTH_LOOPS
async def test_ping_ambiguous_or_invalid_hosts_rejected_without_resolving(
    icmp: FakeIcmp, host: str
) -> None:
    with pytest.raises(NetGuardError):
        await pinger(host, icmp).ping()
    assert icmp.opened == []


@BOTH_LOOPS
async def test_ping_own_address_denied_by_default_policy(icmp: FakeIcmp) -> None:
    # fixed_own_addresses makes 10.255.0.1 "ours".
    icmp.answer("10.255.0.1")
    with pytest.raises(NetGuardError):
        await pinger("10.255.0.1", icmp).ping()
    assert icmp.opened == []


@pytest.mark.parametrize(("address", "expected"), ALLOWED)
@BOTH_LOOPS
async def test_ping_private_ranges_allowed(icmp: FakeIcmp, address: str, expected: str) -> None:
    icmp.answer(expected)
    result = await pinger(address, icmp).ping()
    assert (result.address, result.sent, result.received) == (expected, 1, 1)
    # The socket was opened to the checked address, in its own family.
    assert icmp.addresses == [expected]
    v6 = isinstance(ip_address(expected), IPv6Address)
    assert [p.type for p in icmp.sent] == [128 if v6 else 8]


@BOTH_LOOPS
async def test_ping_public_address_needs_extra_cidr(icmp: FakeIcmp) -> None:
    with pytest.raises(NetGuardError):
        await pinger("192.0.2.30", icmp).ping()
    icmp.answer("192.0.2.30")
    policy = NetPolicy(extra_allowed=parse_cidrs(["192.0.2.0/24"]))
    guard = NetGuard(policy, resolver=no_resolver)
    assert (await pinger("192.0.2.30", icmp, guard=guard).ping()).received == 1


@pytest.mark.parametrize("cidr", ["127.0.0.0/8", "169.254.0.0/16", "0.0.0.0/0"])
@BOTH_LOOPS
async def test_ping_extra_cidrs_never_override_denied(icmp: FakeIcmp, cidr: str) -> None:
    guard = NetGuard(NetPolicy(extra_allowed=parse_cidrs([cidr])), resolver=no_resolver)
    for host in ("127.0.0.1", "169.254.169.254"):
        with pytest.raises(NetGuardError):
            await pinger(host, icmp, guard=guard).ping()
    assert icmp.opened == []


@BOTH_LOOPS
async def test_ping_dns_rebinding_cannot_change_pinged_address(icmp: FakeIcmp) -> None:
    resolver = FakeResolver([PING_OK], ["127.0.0.1"])  # second answer would be loopback
    icmp.answer(PING_OK)
    guard = NetGuard(resolver=resolver)
    result = await pinger("pinged.example.com", icmp, guard=guard, count=3).ping()
    assert result.received == 3
    assert resolver.calls == [("pinged.example.com", 0)]  # resolved exactly once
    assert icmp.addresses == [PING_OK]  # one socket, connected to the checked address
    assert {p.address for p in icmp.sent} == {PING_OK}


@pytest.mark.parametrize(
    "answers",
    [
        [PING_OK, "127.0.0.1"],
        ["169.254.169.254", PING_OK],
        [PING_OK, "::ffff:127.0.0.1"],
        [PING_OK, "192.0.2.1"],
        [PING_OK, "fe80::1%eth0"],
    ],
)
@BOTH_LOOPS
async def test_ping_mixed_answers_denied(icmp: FakeIcmp, answers: list[str]) -> None:
    guard = NetGuard(resolver=FakeResolver(answers))
    with pytest.raises(NetGuardError):
        await pinger("pinged.example.com", icmp, guard=guard).ping()
    assert icmp.opened == []


@BOTH_LOOPS
async def test_ping_never_reaches_a_cloud_host(icmp: FakeIcmp, public_test_net: None) -> None:
    # Public answer, private answer, or literal: a declared cloud host is never pinged.
    for answer in ("192.0.2.50", PING_OK):
        icmp.answer(answer)
        guard = NetGuard(cloud_hosts=["api.example.com"], resolver=FakeResolver([answer]))
        with pytest.raises(NetGuardError, match="cloud host"):
            await pinger("API.example.com.", icmp, guard=guard).ping()
    # An undeclared name resolving to the "public" test net is denied too.
    guard = NetGuard(resolver=FakeResolver(["192.0.2.50"]))
    with pytest.raises(NetGuardError):
        await pinger("other.example.com", icmp, guard=guard).ping()
    assert icmp.opened == []


@BOTH_LOOPS
async def test_ping_resolution_failure(icmp: FakeIcmp) -> None:
    async def failing(host: str, port: int) -> Sequence[str]:
        raise OSError("no such host")

    with pytest.raises(NetGuardError, match="could not resolve"):
        await pinger("missing.example.com", icmp, guard=NetGuard(resolver=failing)).ping()
    assert icmp.opened == []


@BOTH_LOOPS
async def test_ping_sends_well_formed_echo_requests(icmp: FakeIcmp) -> None:
    icmp.answer(PING_OK)
    result = await pinger(PING_OK, icmp, count=3).ping()
    assert (result.sent, result.received, result.loss_ratio) == (3, 3, 0.0)
    assert [p.sequence for p in icmp.sent] == [0, 1, 2]
    assert {(p.type, p.code) for p in icmp.sent} == {(8, 0)}  # echo request, nothing else
    assert len({p.identifier for p in icmp.sent}) == 1
    [nonce] = {p.payload for p in icmp.sent}
    assert len(nonce) == 16
    for packet in icmp.sent:
        assert net_guard._icmp_checksum(packet.raw) == 0  # valid internet checksum
    assert result.rtt_min is not None and result.rtt_max is not None
    assert result.rtt_min <= (result.rtt_avg or 0) <= result.rtt_max
    assert icmp.opened[0].closed


@BOTH_LOOPS
async def test_ping_ipv6_echo(icmp: FakeIcmp) -> None:
    icmp.answer("fd00::30")
    result = await pinger("[fd00::30]", icmp, count=2).ping()
    assert (result.address, result.received) == ("fd00::30", 2)
    assert {(p.type, p.code) for p in icmp.sent} == {(128, 0)}


@BOTH_LOOPS
async def test_ping_nonce_is_fresh_per_run(icmp: FakeIcmp) -> None:
    icmp.answer(PING_OK)
    await pinger(PING_OK, icmp).ping()
    await pinger(PING_OK, icmp).ping()
    assert icmp.sent[0].payload != icmp.sent[1].payload


@BOTH_LOOPS
async def test_ping_counts_loss(icmp: FakeIcmp) -> None:
    icmp.answer(PING_OK, drop(1))
    result = await pinger(PING_OK, icmp, count=3, reply_timeout=0.1).ping()
    assert (result.sent, result.received) == (3, 2)
    assert result.loss_ratio == pytest.approx(1 / 3)
    assert len(result.rtts) == 2


@BOTH_LOOPS
async def test_ping_no_reply_is_a_result_not_an_error(icmp: FakeIcmp) -> None:
    result = await pinger(PING_OK, icmp, count=2, reply_timeout=0.05).ping()
    assert (result.sent, result.received, result.loss_ratio) == (2, 0, 1.0)
    assert (result.rtt_min, result.rtt_avg, result.rtt_max) == (None, None, None)


@BOTH_LOOPS
async def test_ping_ignores_replies_that_are_not_ours(icmp: FakeIcmp) -> None:
    template = net_guard._echo_request(v6=False, identifier=1, sequence=0, payload=b"x" * 16)
    icmp.preload = [
        b"\x00\x00",  # too short
        b"\x00\x00\x00\x00\x00\x01\x00\x00" + b"x" * 16,  # right seq, wrong nonce (forged)
        b"\x00\x00\x00\x00\x00\x01\x00\x05",  # another sequence, no payload
        b"\x03\x01\x00\x00\x00\x00\x00\x00" + b"\x45" * 28,  # ICMP host unreachable
        template,  # an echo *request* reflected back
        b"\x00\x05\x00\x00\x00\x01\x00\x00",  # echo reply with a non-zero code
    ]
    # None of the junk counts; with no real reply, the packet is lost.
    result = await pinger(PING_OK, icmp, reply_timeout=0.1).ping()
    assert result.received == 0
    # With a real reply behind the junk, only that one counts.
    icmp.answer(PING_OK)
    result = await pinger(PING_OK, icmp, count=2).ping()
    assert (result.sent, result.received) == (2, 2)


@BOTH_LOOPS
async def test_ping_late_reply_is_not_counted_for_the_next_packet(icmp: FakeIcmp) -> None:
    # Packet 0's reply arrives while packet 1 waits: it must not count for packet 1.
    # Packet 0 waits 0-0.15 s; packet 1 is sent at 0.2 s and waits until 0.35 s.
    icmp.answer(PING_OK, lambda seq: 0.28 if seq == 0 else None)
    result = await pinger(PING_OK, icmp, count=2, reply_timeout=0.15).ping()
    assert (result.sent, result.received) == (2, 0)


@BOTH_LOOPS
async def test_ping_send_and_receive_errors_count_as_loss(icmp: FakeIcmp) -> None:
    icmp.send_error = OSError(113, "No route to host")
    result = await pinger(PING_OK, icmp, count=2).ping()
    assert (result.sent, result.received) == (2, 0)
    icmp.send_error = None
    icmp.preload = [OSError(113, "No route to host")]
    icmp.answer(PING_OK)
    result = await pinger(PING_OK, icmp).ping()
    assert (result.sent, result.received) == (1, 0)
    assert all(s.closed for s in icmp.opened)


@BOTH_LOOPS
async def test_ping_permission_denied(icmp: FakeIcmp) -> None:
    icmp.open_error = PermissionError(13, "Permission denied")
    with pytest.raises(PingError, match="ping_group_range"):
        await pinger(PING_OK, icmp).ping()
    icmp.open_error = OSError(97, "Address family not supported")
    with pytest.raises(PingError) as info:
        await pinger(PING_OK, icmp).ping()
    assert str(info.value) == "could not open an ICMP socket for 10.0.0.30"
    assert icmp.opened == []


@BOTH_LOOPS
async def test_ping_broadcast_connect_refused_has_its_own_message(icmp: FakeIcmp) -> None:
    # Without SO_BROADCAST the kernel refuses connect() to a broadcast address (EACCES).
    icmp.answer("10.0.0.255")
    icmp.connect_error = PermissionError(13, "Permission denied")
    with pytest.raises(PingError) as info:
        await pinger("10.0.0.255", icmp).ping()
    message = str(info.value)
    assert message == "the system refused to ping 10.0.0.255 (is it a broadcast address?)"
    assert "ping_group_range" not in message  # that hint is for socket creation only
    assert icmp.sent == []
    assert icmp.opened[0].closed


@BOTH_LOOPS
async def test_ping_connect_failure(icmp: FakeIcmp) -> None:
    icmp.connect_error = OSError(101, "Network is unreachable")
    with pytest.raises(PingError) as info:
        await pinger(PING_OK, icmp).ping()
    assert str(info.value) == "could not reach 10.0.0.30 (no route?)"
    assert icmp.sent == []
    assert icmp.opened[0].closed


@BOTH_LOOPS
async def test_ping_reply_from_another_source_is_ignored(icmp: FakeIcmp) -> None:
    # Right sequence and nonce, but from a different host: not counted.
    icmp.answer(PING_OK)
    icmp.reply_source = "10.0.0.31"
    result = await pinger(PING_OK, icmp, count=2, reply_timeout=0.1).ping()
    assert (result.sent, result.received) == (2, 0)
    for source in ("not-an-ip", "fd00::30", "127.0.0.1"):
        icmp.reply_source = source
        assert (await pinger(PING_OK, icmp, reply_timeout=0.1).ping()).received == 0


@BOTH_LOOPS
async def test_ping_reply_source_compared_as_ipv4(icmp: FakeIcmp) -> None:
    icmp.answer(PING_OK)
    icmp.reply_source = f"::ffff:{PING_OK}"  # mapped form of the checked address
    assert (await pinger(PING_OK, icmp).ping()).received == 1


@BOTH_LOOPS
async def test_ping_slow_dns_does_not_shorten_reply_windows(
    monkeypatch: pytest.MonkeyPatch, icmp: FakeIcmp
) -> None:
    # Scaled down: total 0.7 s, DNS may take up to 0.25 s; packets need 2 x 0.2 = 0.4 s.
    monkeypatch.setattr(net_guard, "PING_RESOLVE_TIMEOUT", 0.25)

    async def slow(host: str, port: int) -> Sequence[str]:
        await asyncio.sleep(0.2)  # just under the resolve limit
        return [PING_OK]

    icmp.answer(PING_OK, always(0.17))  # slow replies, inside the 0.2 s reply timeout
    client = pinger(
        "slow.example.com",
        icmp,
        guard=NetGuard(resolver=slow),
        count=2,
        reply_timeout=0.2,
        total_timeout=0.7,
    )
    result = await client.ping()
    assert (result.sent, result.received) == (2, 2)


@BOTH_LOOPS
async def test_ping_resolve_limit_is_separate_from_total(
    monkeypatch: pytest.MonkeyPatch, icmp: FakeIcmp
) -> None:
    monkeypatch.setattr(net_guard, "PING_RESOLVE_TIMEOUT", 0.1)

    async def slow(host: str, port: int) -> Sequence[str]:
        await asyncio.sleep(0.3)  # well within the total, but over the resolve limit
        return [PING_OK]

    client = pinger("slow.example.com", icmp, guard=NetGuard(resolver=slow), total_timeout=5)
    loop = asyncio.get_running_loop()
    started = loop.time()
    with pytest.raises(PingError, match="timed out resolving"):
        await client.ping()
    assert loop.time() - started < 0.25
    assert icmp.opened == []


def test_ping_resolve_limit_leaves_room_for_packets() -> None:
    assert 0 < PING_RESOLVE_TIMEOUT < MAX_PING_TOTAL_TIMEOUT
    assert MIN_PING_INTERVAL <= PING_INTERVAL


@pytest.mark.parametrize(
    "kwargs",
    [
        {"count": 0},
        {"count": MAX_PING_COUNT + 1},
        {"interval": MIN_PING_INTERVAL / 2},
        {"interval": 0},
        {"interval": 60},
        {"reply_timeout": 0},
        {"reply_timeout": 30},
        {"total_timeout": 0},
        {"total_timeout": MAX_PING_TOTAL_TIMEOUT + 1},
    ],
)
def test_ping_caps_enforced(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        GuardedPingClient(NetGuard(resolver=no_resolver), PING_OK, **kwargs)


def test_ping_caps_match_contract() -> None:
    assert MAX_PING_COUNT == 10
    assert MAX_PING_TOTAL_TIMEOUT == DEFAULT_TOTAL_TIMEOUT


@BOTH_LOOPS
async def test_ping_total_time_cap_stops_sending(icmp: FakeIcmp) -> None:
    # Ten packets that each wait up to 5 s would take 50 s; the total cap ends it.
    client = pinger(PING_OK, icmp, count=10, reply_timeout=5, interval=5, total_timeout=0.3)
    loop = asyncio.get_running_loop()
    started = loop.time()
    result = await asyncio.wait_for(client.ping(), 5)
    assert loop.time() - started < 1.0
    assert result.sent == 1 and result.received == 0  # unsent packets aren't counted
    assert icmp.opened[0].closed


@BOTH_LOOPS
async def test_ping_total_time_cap_covers_resolution(icmp: FakeIcmp) -> None:
    async def slow(host: str, port: int) -> Sequence[str]:
        await asyncio.sleep(3600)
        return [PING_OK]

    client = pinger("slow.example.com", icmp, guard=NetGuard(resolver=slow), total_timeout=0.1)
    with pytest.raises(PingError, match="timed out resolving"):
        await asyncio.wait_for(client.ping(), 5)
    assert icmp.opened == []


@BOTH_LOOPS
async def test_ping_socket_closed_on_cancel(icmp: FakeIcmp) -> None:
    task = asyncio.create_task(pinger(PING_OK, icmp, reply_timeout=5).ping())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert icmp.opened[0].closed


@BOTH_LOOPS
async def test_ping_default_opener_is_looked_up_at_call_time(
    monkeypatch: pytest.MonkeyPatch, icmp: FakeIcmp
) -> None:
    monkeypatch.setattr(net_guard, "open_icmp_socket", icmp.open)
    icmp.answer(PING_OK)
    client = GuardedPingClient(NetGuard(resolver=no_resolver), PING_OK, count=1)
    assert (await client.ping()).received == 1


class RecordingSocket:
    """Stands in for socket.socket inside the real opener."""

    created: ClassVar[list[RecordingSocket]] = []

    def __init__(self, family: int, kind: int, proto: int) -> None:
        self.args = (family, kind, proto)
        self.calls: list[tuple[str, Any]] = []
        RecordingSocket.created.append(self)

    def setblocking(self, flag: bool) -> None:
        self.calls.append(("setblocking", flag))

    def connect(self, address: Any) -> None:
        self.calls.append(("connect", address))

    def setsockopt(self, *args: Any) -> None:
        self.calls.append(("setsockopt", args))

    def close(self) -> None:
        self.calls.append(("close", None))


@pytest.mark.parametrize(
    ("address", "expected_args", "sockaddr"),
    [
        (
            "10.0.0.30",
            (socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_ICMP),
            ("10.0.0.30", 0),
        ),
        (
            "fd00::30",
            (socket.AF_INET6, socket.SOCK_DGRAM, socket.IPPROTO_ICMPV6),
            ("fd00::30", 0, 0, 0),
        ),
    ],
)
def test_real_opener_uses_unprivileged_datagram_socket_connected_to_checked_address(
    monkeypatch: pytest.MonkeyPatch,
    address: str,
    expected_args: tuple[int, int, int],
    sockaddr: tuple[Any, ...],
) -> None:
    RecordingSocket.created = []
    monkeypatch.setattr(net_guard.socket, "socket", RecordingSocket)
    opened = net_guard.open_icmp_socket(ip_address(address))
    [sock] = RecordingSocket.created
    assert sock.args == expected_args  # SOCK_DGRAM, never SOCK_RAW
    assert sock.calls == [("setblocking", False)]  # created, not yet connected
    opened.connect()
    assert sock.calls == [("setblocking", False), ("connect", sockaddr)]  # no SO_BROADCAST
    opened.close()
    assert sock.calls[-1] == ("close", None)


def test_no_raw_sockets_or_broadcast_in_net_guard() -> None:
    source = inspect.getsource(net_guard)
    for forbidden in ("socket.SOCK_RAW", "socket.SO_BROADCAST", "setsockopt"):
        assert forbidden not in source


# --- the real socket layer, on both event loops ---------------------------------------
#
# _DatagramIcmpSocket does its own I/O on the running loop. The container runs uvicorn on
# uvloop, which has no loop.sock_recvfrom/sock_sendto (NotImplementedError), so these
# tests run on the default asyncio loop and on uvloop.
#
# Without ICMP permission (most CI), a UDP socket on loopback stands in for the ICMP one:
# same datagram semantics, same non-blocking calls, and a local responder answers echo
# requests. The guard still checks PING_OK; only the test's opener points the socket at
# the stand-in, which the guard itself would never allow. Nothing here changes a default.


def wrap_datagram_socket(sock: socket.socket) -> Any:
    """The real _DatagramIcmpSocket I/O around an already-created datagram socket."""
    sock.setblocking(False)
    wrapper = object.__new__(net_guard._DatagramIcmpSocket)
    wrapper._sock = sock
    wrapper._v4 = True
    return wrapper


class Redirected:
    """Wraps a real socket layer pointed at loopback; reports replies as coming from the
    checked address after recording their real source."""

    def __init__(self, inner: Any, *, connect: bool = True, source: str = PING_OK) -> None:
        self._inner = inner
        self._connect = connect
        self._source = source
        self.sources: list[str] = []
        self.closed = False

    def connect(self) -> None:
        if self._connect:  # a UDP stand-in is already connected to its responder
            self._inner.connect()

    async def send(self, packet: bytes) -> None:
        await self._inner.send(packet)

    async def recv(self) -> tuple[bytes, str]:
        data, source = await self._inner.recv()
        self.sources.append(source)
        return data, self._source

    def close(self) -> None:
        self.closed = True
        self._inner.close()


class EchoResponder(asyncio.DatagramProtocol):
    """Answers ICMP echo requests sent over UDP, like a host would over ICMP."""

    def __init__(
        self, *, drop: Sequence[int] = (), silent: bool = False, forge: bool = False
    ) -> None:
        self.drop = set(drop)
        self.silent = silent
        self.forge = forge  # right sequence, wrong payload (nonce)
        self.requests: list[bytes] = []
        self.transport: Any = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # uvloop's UDPTransport isn't a DatagramTransport subclass

    def datagram_received(self, data: bytes, addr: Any) -> None:
        self.requests.append(data)
        kind, code, _, identifier, sequence = net_guard._ICMP_HEADER.unpack_from(data)
        if self.silent or kind != 8 or code != 0 or sequence in self.drop:
            return
        payload = b"x" * 16 if self.forge else data[8:]
        reply = net_guard._ICMP_HEADER.pack(0, 0, 0, identifier, sequence) + payload
        assert self.transport is not None
        self.transport.sendto(reply, addr)


@pytest.fixture
async def stand_in() -> AsyncIterator[Any]:
    """Starts loopback echo responders; returns (responder, opened, opener)."""
    transports: list[asyncio.DatagramTransport] = []

    async def start(
        source: str = PING_OK, **kwargs: Any
    ) -> tuple[EchoResponder, list[Redirected], Any]:
        loop = asyncio.get_running_loop()
        responder = EchoResponder(**kwargs)
        transport, _ = await loop.create_datagram_endpoint(
            lambda: responder, local_addr=("127.0.0.1", 0)
        )
        transports.append(transport)
        port = transport.get_extra_info("sockname")[1]
        opened: list[Redirected] = []

        def opener(address: Any) -> Redirected:
            assert address == ip_address(PING_OK)  # the guard's checked address
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.connect(("127.0.0.1", port))
            wrapped = Redirected(wrap_datagram_socket(sock), connect=False, source=source)
            opened.append(wrapped)
            return wrapped

        return responder, opened, opener

    yield start
    for transport in transports:
        transport.close()


def stand_in_client(opener: Any, **kwargs: Any) -> GuardedPingClient:
    options: dict[str, Any] = {"count": 1, "interval": MIN_PING_INTERVAL, "reply_timeout": 0.15}
    options.update(kwargs)
    return GuardedPingClient(
        NetGuard(resolver=no_resolver), PING_OK, socket_factory=opener, **options
    )


@BOTH_LOOPS
async def test_real_socket_layer_counts_replies_loss_and_rtt(stand_in: Any) -> None:
    """Regression: on uvloop the client used loop.sock_recvfrom and crashed with
    NotImplementedError after the first echo request."""
    responder, opened, opener = await stand_in(drop=[1])
    result = await stand_in_client(opener, count=3).ping()
    assert (result.address, result.sent, result.received) == (PING_OK, 3, 2)
    assert result.loss_ratio == pytest.approx(1 / 3)
    assert len(result.rtts) == 2
    assert result.rtt_min is not None and result.rtt_avg is not None
    assert result.rtt_max is not None
    assert 0 < result.rtt_min <= result.rtt_avg <= result.rtt_max < 0.15
    # Three well-formed echo requests carrying one nonce; replies read through recvfrom.
    assert [net_guard._ICMP_HEADER.unpack_from(r)[4] for r in responder.requests] == [0, 1, 2]
    assert len({r[8:] for r in responder.requests}) == 1
    [sock] = opened
    assert set(sock.sources) == {"127.0.0.1"}
    assert sock.closed


@BOTH_LOOPS
async def test_real_socket_layer_checks_the_nonce(stand_in: Any) -> None:
    responder, _, opener = await stand_in(forge=True)
    result = await stand_in_client(opener, count=2).ping()
    assert (result.sent, result.received) == (2, 0)
    assert len(responder.requests) == 2  # the forged replies were sent, and ignored


@BOTH_LOOPS
async def test_real_socket_layer_checks_the_reply_source(stand_in: Any) -> None:
    # Real replies with the right nonce, but reported from another address.
    responder, opened, opener = await stand_in(source="10.0.0.31")
    result = await stand_in_client(opener, count=2).ping()
    assert (result.sent, result.received) == (2, 0)
    assert len(responder.requests) == 2
    assert len(opened[0].sources) == 2  # both replies arrived and were refused


@BOTH_LOOPS
async def test_real_socket_layer_timeout_counts_as_lost(stand_in: Any) -> None:
    _, opened, opener = await stand_in(silent=True)
    loop = asyncio.get_running_loop()
    started = loop.time()
    result = await stand_in_client(opener, count=2, reply_timeout=0.1).ping()
    assert (result.sent, result.received, result.loss_ratio) == (2, 0, 1.0)
    assert (result.rtt_min, result.rtt_avg, result.rtt_max) == (None, None, None)
    assert loop.time() - started < 1.0
    assert opened[0].closed


def closed_udp_port() -> int:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.bind(("127.0.0.1", 0))
    port: int = probe.getsockname()[1]
    probe.close()
    return port


@BOTH_LOOPS
async def test_real_socket_layer_unreachable_counts_as_lost(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Nothing listens: the kernel answers with ICMP port unreachable, which the connected
    # socket reports as an error on the next recvfrom (or send).
    port = closed_udp_port()
    opened: list[Redirected] = []

    def opener(address: Any) -> Redirected:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("127.0.0.1", port))
        wrapped = Redirected(wrap_datagram_socket(sock), connect=False)
        opened.append(wrapped)
        return wrapped

    loop = asyncio.get_running_loop()
    started = loop.time()
    with caplog.at_level("DEBUG"):
        result = await stand_in_client(opener, count=3, reply_timeout=2).ping()
    assert (result.sent, result.received) == (3, 0)
    # The error ends each wait at once instead of after the 2 s reply timeout.
    assert loop.time() - started < 2.0
    assert "Traceback" not in caplog.text
    assert opened[0].closed


@BOTH_LOOPS
async def test_real_socket_layer_unregisters_reader_on_cancel() -> None:
    if not hasattr(socket, "AF_UNIX"):
        pytest.skip("needs AF_UNIX datagram sockets")
    loop = asyncio.get_running_loop()
    ours, theirs = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
    with ours, theirs:
        wrapper = wrap_datagram_socket(ours)
        task = asyncio.create_task(wrapper.recv())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # Nothing left registered (asyncio returns False; uvloop always returns None).
        assert loop.remove_reader(ours.fileno()) is not True
        # The socket still works for the next read.
        theirs.send(b"\x00" * 8)
        data, _ = await asyncio.wait_for(wrapper.recv(), 1)
        assert data == b"\x00" * 8


class SendsBusyOnce(socket.socket):
    """A UDP socket whose first send() reports a full buffer (EAGAIN)."""

    busy = True

    def send(self, data: Any, flags: int = 0) -> int:
        if self.busy:
            self.busy = False
            raise BlockingIOError(11, "Resource temporarily unavailable")
        return super().send(data, flags)


@BOTH_LOOPS
async def test_real_socket_layer_waits_until_writable() -> None:
    loop = asyncio.get_running_loop()
    packet = b"\x08" + b"\x00" * 7
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as receiver:
        receiver.bind(("127.0.0.1", 0))
        sender = SendsBusyOnce(socket.AF_INET, socket.SOCK_DGRAM)
        sender.connect(receiver.getsockname())
        wrapper = wrap_datagram_socket(sender)
        try:
            await asyncio.wait_for(wrapper.send(packet), 1)
            assert not sender.busy
            assert loop.remove_writer(sender.fileno()) is not True  # unregistered (asyncio)
            assert receiver.recv(64) == packet
        finally:
            wrapper.close()


@BOTH_LOOPS
async def test_real_socket_strips_ipv4_header() -> None:
    if not hasattr(socket, "AF_UNIX"):
        pytest.skip("needs AF_UNIX datagram sockets")
    reply = b"\x00\x00\x00\x00\x00\x01\x00\x07" + b"n" * 16
    ip_header = b"\x45" + b"\x00" * 19

    async def receive(payload: bytes) -> bytes:
        ours, theirs = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
        with ours, theirs:
            wrapper = wrap_datagram_socket(ours)
            theirs.send(payload)
            data, _source = await wrapper.recv()  # an unnamed AF_UNIX peer: no address
            return bytes(data)

    assert await receive(ip_header + reply) == reply  # macOS-style, with IP header
    assert await receive(reply) == reply  # Linux-style, ICMP only


@BOTH_LOOPS
async def test_real_kernel_loopback_echo() -> None:
    """The real socket layer against this machine's kernel, where unprivileged ICMP
    sockets are allowed (macOS; Linux within ping_group_range). The guard checks a
    private literal; the test's opener then points the socket at loopback, which the
    guard itself would never allow. Replies really come from 127.0.0.1, so the wrapper
    reports them as from the checked address (after recording the real source).
    Skipped where ICMP sockets aren't permitted."""
    loopback = ip_address("127.0.0.1")
    opened: list[Redirected] = []

    def opener(address: Any) -> net_guard.IcmpSocket:
        assert address == ip_address(PING_OK)  # the guard's checked address
        wrapped = Redirected(net_guard.open_icmp_socket(loopback))
        opened.append(wrapped)
        return wrapped

    try:
        result = await stand_in_client(opener, count=3, reply_timeout=1).ping()
    except PingError as exc:
        pytest.skip(f"unprivileged ICMP sockets not available here: {exc}")
    assert (result.sent, result.received, result.loss_ratio) == (3, 3, 0.0)
    assert result.rtt_min is not None and result.rtt_avg is not None
    assert result.rtt_max is not None
    assert 0 < result.rtt_min <= result.rtt_avg <= result.rtt_max < 1
    [sock] = opened
    assert set(sock.sources) == {"127.0.0.1"}  # what recvfrom reported
    assert sock.closed
