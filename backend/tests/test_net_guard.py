"""SSRF guard (Contract 5). No real network: the resolver and the socket layer are fakes.

Only RFC 1918 / RFC 6598 / ULA, RFC 5737 documentation addresses and example.com-style
names appear here. TEST-NET-1 (192.0.2.0/24) stands in for "the public internet" where a
test needs one, by patching the guard's notion of a public address.
"""

from __future__ import annotations

import asyncio
import inspect
import ssl
from collections.abc import AsyncIterator, Sequence
from ipaddress import IPv4Address, IPv4Network, IPv6Address, ip_address
from typing import Any

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
    GuardedSnmpClient,
    NetGuard,
    NetGuardError,
    NetPolicy,
    SnmpError,
    SnmpV2c,
    SnmpV3,
    check_address,
    guarded_http_client,
    parse_cidrs,
    parse_host,
)

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
