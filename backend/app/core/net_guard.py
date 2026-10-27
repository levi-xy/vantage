"""SSRF guard: the only way Vantage opens connections to devices and cloud APIs.

Contract 5 (AGENTS.md):

* A hostname is resolved **once**; every resolved address is checked, and the
  connection goes to that checked address. A second DNS answer can never change
  where we connect (DNS rebinding).
* Allowed by default: RFC 1918, RFC 6598 (CGNAT) and IPv6 ULA (``fc00::/7``).
* Always denied, whatever else is configured: loopback, link-local, ``0.0.0.0/8``,
  multicast, cloud metadata addresses and the Vantage host's own addresses.
  IPv4-mapped IPv6 addresses are judged as the IPv4 address they map to, and
  addresses embedding an IPv4 address (NAT64, 6to4) are denied when that IPv4
  address is.
* Extra allowed CIDRs are configurable (admin-only setting).
* The public internet is reachable only for an integration's declared
  ``cloud_hosts``, over HTTPS, with certificate verification on. A cloud host must
  resolve to public addresses only; any non-public answer denies it.
* Redirects are never followed automatically. If a caller opts in per request,
  every hop still goes through the transport and is re-checked.
* Every request has timeouts: connect 5 s, total 15 s by default.

* ICMP echo (ping) follows the same rules: resolve once, check every answer, ping
  only the checked address, never a cloud host. It uses unprivileged ICMP datagram
  sockets (no raw sockets) and caps the packet count and the total time.

Integrations never build their own clients: they get a :class:`NetGuard`-backed
HTTP client (:func:`guarded_http_client`), SNMP client (:class:`GuardedSnmpClient`)
or ping client (:class:`GuardedPingClient`) through their instance context.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import re
import secrets
import socket
import ssl
import struct
from collections.abc import (
    AsyncIterable,
    AsyncIterator,
    Awaitable,
    Callable,
    Iterable,
    Iterator,
    Sequence,
)
from dataclasses import dataclass, field
from ipaddress import IPv4Address, IPv4Network, IPv6Address, IPv6Network
from typing import Any, Literal, NamedTuple, Protocol

import httpcore
import httpx
from pyasn1.type import univ
from pydantic import SecretStr
from pysnmp.hlapi.v3arch.asyncio import (
    USM_AUTH_HMAC96_MD5,
    USM_AUTH_HMAC96_SHA,
    USM_AUTH_HMAC128_SHA224,
    USM_AUTH_HMAC192_SHA256,
    USM_AUTH_HMAC256_SHA384,
    USM_AUTH_HMAC384_SHA512,
    USM_AUTH_NONE,
    USM_PRIV_CBC56_DES,
    USM_PRIV_CFB128_AES,
    USM_PRIV_CFB192_AES,
    USM_PRIV_CFB256_AES,
    USM_PRIV_NONE,
    CommunityData,
    ContextData,
    ObjectIdentity,
    ObjectType,
    SnmpEngine,
    Udp6TransportTarget,
    UdpTransportTarget,
    UsmUserData,
    get_cmd,
    walk_cmd,
)
from pysnmp.proto.rfc1902 import IpAddress

IPAddress = IPv4Address | IPv6Address
IPNetwork = IPv4Network | IPv6Network

DEFAULT_CONNECT_TIMEOUT = 5.0
DEFAULT_TOTAL_TIMEOUT = 15.0


class NetGuardError(Exception):
    """A connection was refused by the guard.

    Messages name the host as configured (not a secret) and the reason; they never
    include credentials, URLs with query strings or request bodies.
    """


# --- address policy ---------------------------------------------------------------

DEFAULT_ALLOWED: tuple[IPNetwork, ...] = (
    IPv4Network("10.0.0.0/8"),  # RFC 1918
    IPv4Network("172.16.0.0/12"),  # RFC 1918
    IPv4Network("192.168.0.0/16"),  # RFC 1918
    IPv4Network("100.64.0.0/10"),  # RFC 6598 (CGNAT)
    IPv6Network("fc00::/7"),  # IPv6 ULA
)

ALWAYS_DENIED: tuple[IPNetwork, ...] = (
    IPv4Network("0.0.0.0/8"),  # "this network"
    IPv4Network("127.0.0.0/8"),  # loopback
    IPv4Network("169.254.0.0/16"),  # link-local, incl. 169.254.169.254 metadata
    IPv4Network("224.0.0.0/4"),  # multicast
    IPv4Network("240.0.0.0/4"),  # reserved, incl. 255.255.255.255 broadcast
    IPv4Network("100.100.100.200/32"),  # Alibaba Cloud metadata (inside CGNAT)
    IPv4Network("168.63.129.16/32"),  # Azure platform endpoint (wireserver)
    IPv6Network("::/96"),  # unspecified, loopback and deprecated IPv4-compatible
    IPv6Network("fe80::/10"),  # link-local
    IPv6Network("fec0::/10"),  # deprecated site-local
    IPv6Network("ff00::/8"),  # multicast
    IPv6Network("fd00:ec2::254/128"),  # AWS metadata over IPv6 (inside ULA)
)

_NAT64 = (
    IPv6Network("64:ff9b::/96"),  # RFC 6052 well-known prefix
    IPv6Network("64:ff9b:1::/48"),  # RFC 8215 local-use prefix (IPv4 judged as the last 32 bits)
)
_SIXTOFOUR = IPv6Network("2002::/16")

# Never acceptable as a cloud host's address, even when is_global says "public":
# NAT64 and 6to4 addresses can carry a private IPv4 address and reach the LAN through a
# translator, and 192.0.0.0/24 (IETF protocol assignments) has global-looking entries.
_CLOUD_DENIED: tuple[IPNetwork, ...] = (*_NAT64, _SIXTOFOUR, IPv4Network("192.0.0.0/24"))


def parse_cidrs(values: Iterable[str]) -> tuple[IPNetwork, ...]:
    """Parse admin-supplied extra allowed CIDRs. Host bits must be zero.

    The always-denied ranges still win over anything listed here.
    """
    networks: list[IPNetwork] = []
    for raw in values:
        try:
            networks.append(ipaddress.ip_network(raw.strip(), strict=True))
        except ValueError:
            raise ValueError(f"not a valid CIDR: {raw.strip()[:64]!r}") from None
    return tuple(networks)


@dataclass(frozen=True)
class NetPolicy:
    """Which addresses may be reached.

    ``own_addresses`` defaults to :func:`discover_own_addresses`, so forgetting to
    pass it can never silently drop the "own addresses" denial. Pass an explicit set
    only to add to or replace the discovered one deliberately.
    """

    # TODO(maintainer): load from the settings table once the admin-only settings API
    # exists (key name and shape to be decided there); use parse_cidrs() to validate.
    extra_allowed: tuple[IPNetwork, ...] = ()
    own_addresses: frozenset[IPAddress] = field(
        default_factory=lambda: discover_own_addresses()  # late-bound, patchable in tests
    )


def discover_own_addresses() -> frozenset[IPAddress]:
    """Best-effort list of this host's addresses. Sends no packets.

    Uses the addresses the hostname resolves to and the source address the kernel
    would pick for an outbound route (a UDP ``connect`` only selects a route).
    """
    found: set[IPAddress] = set()
    with contextlib.suppress(OSError):
        for info in socket.getaddrinfo(socket.gethostname(), None):
            with contextlib.suppress(ValueError):
                found.add(_parse_ip(str(info[4][0])))
    # RFC 5737 / RFC 3849 documentation addresses: never actually contacted.
    for family, probe in ((socket.AF_INET, "192.0.2.1"), (socket.AF_INET6, "2001:db8::1")):
        with (
            contextlib.suppress(OSError, ValueError),
            socket.socket(family, socket.SOCK_DGRAM) as s,
        ):
            s.connect((probe, 9))
            found.add(_parse_ip(str(s.getsockname()[0])))
    # Inside Docker this sees the container's addresses only, not the Docker host's
    # LAN address (documented in docs/SECURITY.md, "Network guard").
    return frozenset(_unwrap(a) for a in found)


def _parse_ip(text: str) -> IPAddress:
    # Strip any IPv6 zone id ("fe80::1%eth0"); the address alone decides.
    return ipaddress.ip_address(text.split("%", 1)[0])


def _unwrap(address: IPAddress) -> IPAddress:
    """An IPv4-mapped IPv6 address is the IPv4 address it maps to."""
    if isinstance(address, IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def _embedded_ipv4(address: IPv6Address) -> IPv4Address | None:
    if address.sixtofour is not None:
        return address.sixtofour
    if any(address in net for net in _NAT64):
        return IPv4Address(int(address) & 0xFFFFFFFF)
    return None


def check_address(address: IPAddress, policy: NetPolicy, *, cloud_host: bool) -> IPAddress:
    """Return the address to connect to, or raise :class:`NetGuardError`.

    ``cloud_host`` is true only for a declared cloud host reached over HTTPS. A cloud
    host must resolve to public addresses only: a private, CGNAT, ULA or otherwise
    non-global answer is denied, so a cloud host name can't be pointed at the LAN.
    """
    addr = _unwrap(address)
    if _is_denied(addr, policy):
        raise NetGuardError(f"address {addr} is not allowed")
    if cloud_host:
        if any(addr in net for net in _CLOUD_DENIED):
            raise NetGuardError(f"cloud host resolved to non-public address {addr}")
        if _is_public(addr):
            return addr
        raise NetGuardError(f"cloud host resolved to non-public address {addr}")
    if any(addr in net for net in DEFAULT_ALLOWED + policy.extra_allowed):
        return addr
    raise NetGuardError(f"address {addr} is not in an allowed range")


def _is_public(addr: IPAddress) -> bool:
    return addr.is_global


def _is_denied(addr: IPAddress, policy: NetPolicy) -> bool:
    if addr in policy.own_addresses:
        return True
    if any(addr in net for net in ALWAYS_DENIED):
        return True
    if isinstance(addr, IPv6Address):
        if addr.scope_id:
            return True
        embedded = _embedded_ipv4(addr)
        if embedded is not None and _is_denied(embedded, policy):
            return True
    return False


# --- hostnames --------------------------------------------------------------------

_LABEL = re.compile(r"^[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?$")
# A label inet_aton() would read as a number: decimal, octal (leading 0) or hex.
_NUMERIC_LABEL = re.compile(r"^(?:0x[0-9a-f]*|[0-9]+)$")


def parse_host(host: str) -> IPAddress | str:
    """Validate a host as configured: a strict IP literal or a plain DNS name.

    Ambiguous numeric forms (``2130706433``, ``0x7f.1``, ``0177.0.0.1``, ``10.1``)
    are rejected outright instead of being handed to a resolver that might read them
    as an address.
    """
    text = host.strip().lower()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    if "%" in text:
        raise NetGuardError("IPv6 zone ids are not allowed")
    with contextlib.suppress(ValueError):
        return ipaddress.ip_address(text)
    if text.endswith("."):
        text = text[:-1]
    if not text or len(text) > 253 or not text.isascii():
        raise NetGuardError("invalid host name")
    labels = text.split(".")
    if not all(_LABEL.match(label) for label in labels):
        raise NetGuardError("invalid host name")
    if _NUMERIC_LABEL.match(labels[-1]):
        raise NetGuardError("ambiguous numeric host; write the address in dotted form")
    return text


Resolver = Callable[[str, int], Awaitable[Sequence[str]]]


async def system_resolver(host: str, port: int) -> Sequence[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


class NetGuard:
    """Resolves and checks a destination. One per integration instance."""

    def __init__(
        self,
        policy: NetPolicy | None = None,
        *,
        cloud_hosts: Iterable[str] = (),
        resolver: Resolver = system_resolver,
    ) -> None:
        self.policy = policy or NetPolicy()
        hosts = set()
        for name in cloud_hosts:
            parsed = parse_host(name)
            if not isinstance(parsed, str):
                raise ValueError("cloud_hosts must be host names, not addresses")
            hosts.add(parsed)
        self.cloud_hosts: frozenset[str] = frozenset(hosts)
        self._resolver = resolver

    def is_cloud_host(self, host: str) -> bool:
        parsed = parse_host(host)
        return isinstance(parsed, str) and parsed in self.cloud_hosts

    async def resolve(self, host: str, port: int, *, cloud_host: bool = False) -> IPAddress:
        """Resolve ``host`` once, check every answer, return the address to use.

        If any answer is denied the whole host is denied: an attacker controlling DNS
        must not be able to slip a bad address in next to a good one.
        """
        if not 0 < port < 65536:
            raise NetGuardError("invalid port")
        return await self._resolve_checked(host, port, cloud_host=cloud_host)

    async def resolve_for_ping(self, host: str) -> IPAddress:
        """:meth:`resolve` for ICMP, which has no port.

        Never a cloud host: a declared cloud host name is refused outright, and every
        other name gets no cloud exemption, so only allowed private ranges pass.
        """
        if self.is_cloud_host(host):
            raise NetGuardError(f"{parse_host(host)} is a cloud host and can't be pinged")
        return await self._resolve_checked(host, 0, cloud_host=False)

    async def _resolve_checked(self, host: str, port: int, *, cloud_host: bool) -> IPAddress:
        target = parse_host(host)
        if not isinstance(target, str):
            return check_address(target, self.policy, cloud_host=cloud_host)
        try:
            answers = await self._resolver(target, port)
        except OSError:
            raise NetGuardError(f"could not resolve {target}") from None
        if not answers:
            raise NetGuardError(f"could not resolve {target}")
        checked: list[IPAddress] = []
        for answer in answers:
            try:
                address = _parse_ip(answer)
            except ValueError:
                raise NetGuardError(f"resolver returned an invalid address for {target}") from None
            if isinstance(address, IPv6Address) and address.scope_id:
                raise NetGuardError(f"address {address} is not allowed")
            checked.append(check_address(address, self.policy, cloud_host=cloud_host))
        return checked[0]


# --- HTTP -------------------------------------------------------------------------


class _GuardedNetworkBackend(httpcore.AsyncNetworkBackend):
    """Resolves and checks at connect time, then connects to the checked address.

    httpcore still uses the URL host for TLS SNI and certificate verification, and
    the Host header comes from the URL, so only the socket's destination changes.
    """

    def __init__(self, guard: NetGuard, inner: httpcore.AsyncNetworkBackend | None = None):
        self._guard = guard
        self._inner = inner or httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109 - httpcore's interface
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        # The transport only lets a cloud host through over verified HTTPS.
        cloud_host = self._guard.is_cloud_host(host)
        try:
            async with asyncio.timeout(timeout):
                address = await self._guard.resolve(host, port, cloud_host=cloud_host)
        except TimeoutError:
            raise httpcore.ConnectTimeout(f"timed out resolving {host}") from None
        return await self._inner.connect_tcp(
            str(address),
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,  # noqa: ASYNC109 - httpcore's interface
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        raise NetGuardError("unix sockets are not allowed")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


# Most specific first.
_HTTPCORE_EXCEPTIONS: tuple[tuple[type[Exception], type[httpx.TransportError]], ...] = (
    (httpcore.ConnectTimeout, httpx.ConnectTimeout),
    (httpcore.ReadTimeout, httpx.ReadTimeout),
    (httpcore.WriteTimeout, httpx.WriteTimeout),
    (httpcore.PoolTimeout, httpx.PoolTimeout),
    (httpcore.TimeoutException, httpx.TimeoutException),
    (httpcore.ConnectError, httpx.ConnectError),
    (httpcore.ReadError, httpx.ReadError),
    (httpcore.WriteError, httpx.WriteError),
    (httpcore.NetworkError, httpx.NetworkError),
    (httpcore.ProxyError, httpx.ProxyError),
    (httpcore.UnsupportedProtocol, httpx.UnsupportedProtocol),
    (httpcore.LocalProtocolError, httpx.LocalProtocolError),
    (httpcore.RemoteProtocolError, httpx.RemoteProtocolError),
    (httpcore.ProtocolError, httpx.ProtocolError),
)


@contextlib.contextmanager
def _map_httpcore_errors(request: httpx.Request) -> Iterator[None]:
    try:
        yield
    except Exception as exc:
        for source, target in _HTTPCORE_EXCEPTIONS:
            if isinstance(exc, source):
                raise target(str(exc), request=request) from exc
        raise


def _ssl_verified(context: ssl.SSLContext) -> bool:
    return context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname


class _DeadlineStream(httpx.AsyncByteStream):
    """Response body that must finish before the request's total deadline."""

    def __init__(self, inner: AsyncIterable[bytes], deadline: float, request: httpx.Request):
        self._inner = inner
        self._deadline = deadline
        self._request = request

    async def __aiter__(self) -> AsyncIterator[bytes]:
        chunks = aiter(self._inner)
        while True:
            try:
                async with asyncio.timeout_at(self._deadline):
                    with _map_httpcore_errors(self._request):
                        chunk = await anext(chunks)
            except StopAsyncIteration:
                return
            except TimeoutError:
                raise httpx.ReadTimeout(
                    "response exceeded the total time limit", request=self._request
                ) from None
            yield chunk

    async def aclose(self) -> None:
        close = getattr(self._inner, "aclose", None)
        if close is not None:
            await close()


class GuardedTransport(httpx.AsyncBaseTransport):
    """httpx transport that enforces the guard on every request, including each
    redirect hop a caller chooses to follow."""

    def __init__(
        self,
        guard: NetGuard,
        *,
        verify: bool | ssl.SSLContext = True,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        total_timeout: float = DEFAULT_TOTAL_TIMEOUT,
        network_backend: httpcore.AsyncNetworkBackend | None = None,
    ) -> None:
        if connect_timeout <= 0 or total_timeout <= 0:
            raise ValueError("timeouts must be positive")
        self._guard = guard
        self._ssl = (
            verify
            if isinstance(verify, ssl.SSLContext)
            else httpx.create_ssl_context(verify=verify)
        )
        self._connect_timeout = connect_timeout
        self._total_timeout = total_timeout
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=self._ssl,
            network_backend=_GuardedNetworkBackend(guard, network_backend),
            retries=0,
        )

    def _timeouts(self, requested: Any) -> dict[str, float]:
        """Per-phase timeouts, never longer than ours and never disabled."""
        caps = {
            "connect": self._connect_timeout,
            "read": self._total_timeout,
            "write": self._total_timeout,
            "pool": self._total_timeout,
        }
        result: dict[str, float] = {}
        for key, cap in caps.items():
            value = requested.get(key) if isinstance(requested, dict) else None
            result[key] = cap if not isinstance(value, int | float) else min(float(value), cap)
        return result

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        if url.scheme not in ("http", "https"):
            raise NetGuardError("only http and https are allowed")
        # The ASCII (punycode) form: url.host decodes IDN labels to Unicode, but the
        # connection, SNI and resolver all use the raw ASCII host.
        host = url.raw_host.decode("ascii")
        parse_host(host)
        if self._guard.is_cloud_host(host) and (
            url.scheme != "https" or not _ssl_verified(self._ssl)
        ):
            raise NetGuardError(f"{host} must be reached over HTTPS with certificate verification")
        if not isinstance(request.stream, AsyncIterable):
            raise TypeError("the guarded transport needs an async request body")
        core_request = httpcore.Request(
            method=request.method,
            url=httpcore.URL(
                scheme=url.raw_scheme, host=url.raw_host, port=url.port, target=url.raw_path
            ),
            headers=request.headers.raw,
            content=request.stream,
            extensions={
                **request.extensions,
                "timeout": self._timeouts(request.extensions.get("timeout")),
            },
        )
        deadline = asyncio.get_running_loop().time() + self._total_timeout
        try:
            async with asyncio.timeout_at(deadline):
                with _map_httpcore_errors(request):
                    response = await self._pool.handle_async_request(core_request)
        except TimeoutError:
            raise httpx.TimeoutException(
                "request exceeded the total time limit", request=request
            ) from None
        if not isinstance(response.stream, AsyncIterable):  # pragma: no cover - httpcore invariant
            raise TypeError("unexpected synchronous response stream")
        return httpx.Response(
            status_code=response.status,
            headers=response.headers,
            stream=_DeadlineStream(response.stream, deadline, request),
            extensions=response.extensions,
        )

    async def aclose(self) -> None:
        await self._pool.aclose()


def guarded_http_client(
    guard: NetGuard,
    *,
    verify: bool | ssl.SSLContext = True,
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
    total_timeout: float = DEFAULT_TOTAL_TIMEOUT,
    base_url: str = "",
    headers: dict[str, str] | None = None,
    network_backend: httpcore.AsyncNetworkBackend | None = None,
) -> httpx.AsyncClient:
    """An ``httpx.AsyncClient`` whose every connection goes through ``guard``.

    Environment proxies and ``.netrc`` are ignored (``trust_env=False``): a proxy
    would make the guard check the proxy instead of the device. Redirects are not
    followed by default; a caller passing ``follow_redirects=True`` still has every
    hop checked by the transport.

    ``verify=False`` is for devices with self-signed certificates on private
    networks; cloud hosts are then refused. ``network_backend`` is for tests.
    """
    transport = GuardedTransport(
        guard,
        verify=verify,
        connect_timeout=connect_timeout,
        total_timeout=total_timeout,
        network_backend=network_backend,
    )
    return httpx.AsyncClient(
        transport=transport,
        trust_env=False,
        follow_redirects=False,
        timeout=httpx.Timeout(total_timeout, connect=connect_timeout),
        base_url=base_url,
        headers=headers,
    )


# --- SNMP (read-only: GET and WALK only, never SET) -------------------------------


class SnmpError(Exception):
    """An SNMP request failed. The message never contains the community or keys."""


@dataclass(frozen=True)
class SnmpV2c:
    community: SecretStr


_AUTH_PROTOCOLS = {
    "md5": USM_AUTH_HMAC96_MD5,
    "sha": USM_AUTH_HMAC96_SHA,
    "sha224": USM_AUTH_HMAC128_SHA224,
    "sha256": USM_AUTH_HMAC192_SHA256,
    "sha384": USM_AUTH_HMAC256_SHA384,
    "sha512": USM_AUTH_HMAC384_SHA512,
}
_PRIV_PROTOCOLS = {
    "des": USM_PRIV_CBC56_DES,
    "aes128": USM_PRIV_CFB128_AES,
    "aes192": USM_PRIV_CFB192_AES,
    "aes256": USM_PRIV_CFB256_AES,
}


@dataclass(frozen=True)
class SnmpV3:
    username: str
    auth_key: SecretStr | None = None
    priv_key: SecretStr | None = None
    auth_protocol: Literal["md5", "sha", "sha224", "sha256", "sha384", "sha512"] = "sha"
    priv_protocol: Literal["des", "aes128", "aes192", "aes256"] = "aes128"


class SnmpVarBind(NamedTuple):
    oid: str
    # int for numeric types, str for OIDs and IP addresses, bytes for octet strings,
    # None for noSuchObject / noSuchInstance / endOfMibView.
    value: int | str | bytes | None


_OID = re.compile(r"^\.?[0-9]+(?:\.[0-9]+)*$")


def _oid(text: str) -> ObjectType:
    if not _OID.match(text):
        raise ValueError("OIDs must be numeric, e.g. 1.3.6.1.2.1.1.1.0")
    return ObjectType(ObjectIdentity(text.lstrip(".")))


def _convert(value: Any) -> int | str | bytes | None:
    if isinstance(value, univ.Null):  # noSuchObject, noSuchInstance, endOfMibView
        return None
    if isinstance(value, univ.Integer):  # Integer32, Counter32/64, Gauge32, TimeTicks
        return int(value)
    if isinstance(value, univ.ObjectIdentifier):
        return str(value)
    if isinstance(value, univ.OctetString):
        if value.tagSet == _IP_ADDRESS_TAGSET:
            return str(value.prettyPrint())
        return bytes(value)
    return str(value.prettyPrint())


_IP_ADDRESS_TAGSET = IpAddress.tagSet


def _checked_target_class(base: Any, sockaddr: tuple[Any, ...]) -> Any:
    """A pysnmp transport target pinned to an already-checked address.

    pysnmp normally resolves the host itself; this override hands it the address the
    guard checked, so it never does its own DNS lookup.
    """

    class _Checked(base):  # type: ignore[misc]
        async def _resolve_address(self, transport_addr: tuple[Any, ...]) -> tuple[Any, ...]:
            return sockaddr

    return _Checked


class GuardedSnmpClient:
    """Read-only SNMP client for one device. Exposes GET and WALK only."""

    def __init__(
        self,
        guard: NetGuard,
        host: str,
        auth: SnmpV2c | SnmpV3,
        *,
        port: int = 161,
        timeout: float = DEFAULT_CONNECT_TIMEOUT,
        retries: int = 2,
        total_timeout: float = DEFAULT_TOTAL_TIMEOUT,
    ) -> None:
        if timeout <= 0 or total_timeout <= 0 or retries < 0:
            raise ValueError("timeouts must be positive and retries non-negative")
        self._guard = guard
        self._host = host
        self._port = port
        self._auth = auth
        self._timeout = timeout
        self._retries = retries
        self._total_timeout = total_timeout
        self._engine: Any = None

    def _auth_data(self) -> Any:
        auth = self._auth
        if isinstance(auth, SnmpV2c):
            return CommunityData(auth.community.get_secret_value(), mpModel=1)  # noqa: S508 - v2c is what most devices offer; v3 is supported
        auth_key = auth.auth_key.get_secret_value() if auth.auth_key else None
        priv_key = auth.priv_key.get_secret_value() if auth.priv_key else None
        if priv_key and not auth_key:
            raise ValueError("SNMPv3 privacy requires an authentication key")
        return UsmUserData(
            auth.username,
            authKey=auth_key,
            privKey=priv_key,
            authProtocol=_AUTH_PROTOCOLS[auth.auth_protocol] if auth_key else USM_AUTH_NONE,
            privProtocol=_PRIV_PROTOCOLS[auth.priv_protocol] if priv_key else USM_PRIV_NONE,
        )

    async def _target(self) -> Any:
        # SNMP never goes to the public internet: no cloud exemption.
        address = await self._guard.resolve(self._host, self._port, cloud_host=False)
        if isinstance(address, IPv4Address):
            cls = _checked_target_class(UdpTransportTarget, (str(address), self._port))
        else:
            cls = _checked_target_class(Udp6TransportTarget, (str(address), self._port, 0, 0))
        return await cls.create(
            (str(address), self._port), timeout=self._timeout, retries=self._retries
        )

    def _snmp_engine(self) -> Any:
        if self._engine is None:
            self._engine = SnmpEngine()
        return self._engine

    async def get(self, oids: Sequence[str]) -> list[SnmpVarBind]:
        """SNMP GET of one or more numeric OIDs."""
        if not oids:
            return []
        var_binds = [_oid(o) for o in oids]
        target = await self._target()
        try:
            async with asyncio.timeout(self._total_timeout):
                error_indication, error_status, _, result = await get_cmd(
                    self._snmp_engine(),
                    self._auth_data(),
                    target,
                    ContextData(),
                    *var_binds,
                    lookupMib=False,
                )
        except TimeoutError:
            raise SnmpError("SNMP request timed out") from None
        _raise_for_errors(error_indication, error_status)
        return [SnmpVarBind(str(name), _convert(value)) for name, value in result]

    async def walk(self, oid: str, *, max_rows: int = 10_000) -> list[SnmpVarBind]:
        """Walk the subtree under ``oid`` (stops at the end of the subtree)."""
        start = _oid(oid)
        target = await self._target()
        rows: list[SnmpVarBind] = []
        try:
            async with asyncio.timeout(self._total_timeout):
                async for error_indication, error_status, _, result in walk_cmd(
                    self._snmp_engine(),
                    self._auth_data(),
                    target,
                    ContextData(),
                    start,
                    lookupMib=False,
                    lexicographicMode=False,
                    maxRows=max_rows,
                ):
                    _raise_for_errors(error_indication, error_status)
                    rows.extend(SnmpVarBind(str(n), _convert(v)) for n, v in result)
        except TimeoutError:
            raise SnmpError("SNMP walk timed out") from None
        return rows

    def close(self) -> None:
        if self._engine is not None:
            self._engine.close_dispatcher()
            self._engine = None


def _raise_for_errors(error_indication: Any, error_status: Any) -> None:
    if error_indication:
        raise SnmpError(f"SNMP error: {error_indication}")
    if error_status:
        raise SnmpError(f"SNMP error status: {error_status.prettyPrint()}")


# --- ICMP echo (ping) -------------------------------------------------------------
#
# Maintainer-approved (Contract 5). Unprivileged ICMP *datagram* sockets only
# (``SOCK_DGRAM`` + ``IPPROTO_ICMP``/``IPPROTO_ICMPV6``): on Linux the kernel allows
# them for groups listed in the ``net.ipv4.ping_group_range`` sysctl, with no raw-socket
# capability. The socket is connected to the checked address, so packets go only there.
# A reply counts only if it comes from the checked address and echoes this run's random
# payload nonce and the expected sequence number, whatever else the kernel delivers.
# ``SO_BROADCAST`` is never set, so the kernel refuses to connect to a (subnet) broadcast
# address it knows about (EACCES).

MAX_PING_COUNT = 10
PING_INTERVAL = 1.0  # default seconds between echo requests (what ctx.ping_client() uses)
MIN_PING_INTERVAL = 0.2  # Linux's own floor for non-root
MAX_PING_INTERVAL = 5.0
MAX_PING_REPLY_TIMEOUT = 5.0
MAX_PING_TOTAL_TIMEOUT = DEFAULT_TOTAL_TIMEOUT
# DNS gets at most this much of the total, so the rest is left for the packets: a caller
# whose packets fit in MAX_PING_TOTAL_TIMEOUT - PING_RESOLVE_TIMEOUT is never cut short.
PING_RESOLVE_TIMEOUT = DEFAULT_CONNECT_TIMEOUT

_ICMP_ECHO_REQUEST = 8
_ICMP_ECHO_REPLY = 0
_ICMPV6_ECHO_REQUEST = 128
_ICMPV6_ECHO_REPLY = 129
_ICMP_HEADER = struct.Struct("!BBHHH")  # type, code, checksum, identifier, sequence
_PING_NONCE_BYTES = 16
_MAX_ICMP_DATAGRAM = 2048


class PingError(Exception):
    """The ping could not be run (no ICMP permission, no socket, timed out resolving).

    Messages are fixed text plus, at most, the checked address: safe to display.
    """


class IcmpSocket(Protocol):
    """An ICMP datagram socket for one checked address."""

    def connect(self) -> None:
        """Connect to the checked address (raises ``PermissionError`` for a broadcast
        address, since ``SO_BROADCAST`` is never set)."""
        ...

    async def send(self, packet: bytes) -> None: ...

    async def recv(self) -> tuple[bytes, str]:
        """One ICMP message, starting at the ICMP header (no IP header), and the
        address it came from."""
        ...

    def close(self) -> None: ...


IcmpSocketFactory = Callable[[IPAddress], IcmpSocket]


class _DatagramIcmpSocket:
    """The real socket layer: an unprivileged ICMP datagram socket, never a raw one."""

    def __init__(self, address: IPAddress) -> None:
        self._v4 = isinstance(address, IPv4Address)
        if self._v4:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_ICMP)
            self._sockaddr: tuple[Any, ...] = (str(address), 0)
        else:
            sock = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM, socket.IPPROTO_ICMPV6)
            self._sockaddr = (str(address), 0, 0, 0)
        try:
            sock.setblocking(False)
        except BaseException:
            sock.close()
            raise
        self._sock = sock

    def connect(self) -> None:
        # Connected: every send goes to this checked address and nowhere else.
        self._sock.connect(self._sockaddr)

    async def send(self, packet: bytes) -> None:
        await asyncio.get_running_loop().sock_sendall(self._sock, packet)

    async def recv(self) -> tuple[bytes, str]:
        data, source = await asyncio.get_running_loop().sock_recvfrom(
            self._sock, _MAX_ICMP_DATAGRAM
        )
        # Linux hands a ping socket the ICMP message alone; macOS includes the IPv4
        # header. An ICMP message never starts with 0x4_, an IPv4 header always does.
        if self._v4 and len(data) >= 20 and data[0] >> 4 == 4:
            data = data[(data[0] & 0x0F) * 4 :]
        return data, str(source[0]) if isinstance(source, tuple) and source else ""

    def close(self) -> None:
        self._sock.close()


def open_icmp_socket(address: IPAddress) -> IcmpSocket:
    """Create (not yet connect) the real ICMP datagram socket for an address the guard
    has checked.

    Only :class:`GuardedPingClient` calls this, after the guard's check. Tests replace
    it to fake the socket layer, keeping the guard in the loop.
    """
    return _DatagramIcmpSocket(address)


def _same_address(source: str, address: IPAddress) -> bool:
    try:
        return _unwrap(_parse_ip(source)) == address
    except ValueError:
        return False


def _icmp_checksum(data: bytes) -> int:
    if len(data) % 2:
        data += b"\x00"
    total = sum(int(word) for word in struct.unpack(f"!{len(data) // 2}H", data))
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return ~total & 0xFFFF


def _echo_request(*, v6: bool, identifier: int, sequence: int, payload: bytes) -> bytes:
    kind = _ICMPV6_ECHO_REQUEST if v6 else _ICMP_ECHO_REQUEST
    header = _ICMP_HEADER.pack(kind, 0, 0, identifier, sequence)
    if v6:
        return header + payload  # the kernel fills the ICMPv6 checksum (pseudo-header)
    checksum = _icmp_checksum(header + payload)
    return _ICMP_HEADER.pack(kind, 0, checksum, identifier, sequence) + payload


def _echo_reply(data: bytes, *, v6: bool) -> tuple[int, bytes] | None:
    """``(sequence, payload)`` of an echo reply, or ``None`` for anything else.

    The identifier is not checked: Linux rewrites it to the socket's own id. The
    payload carries a random per-run nonce instead, which the caller compares.
    """
    if len(data) < _ICMP_HEADER.size:
        return None
    kind, code, _, _, sequence = _ICMP_HEADER.unpack_from(data)
    if code != 0 or kind != (_ICMPV6_ECHO_REPLY if v6 else _ICMP_ECHO_REPLY):
        return None
    return sequence, data[_ICMP_HEADER.size :]


@dataclass(frozen=True)
class PingResult:
    """What one ping run measured. Round-trip times are in seconds."""

    address: str  # the checked address that was pinged
    sent: int
    received: int
    rtts: tuple[float, ...]

    @property
    def loss_ratio(self) -> float:
        """Share of sent packets with no reply, 0.0 to 1.0."""
        return 1.0 - self.received / self.sent if self.sent else 1.0

    @property
    def rtt_min(self) -> float | None:
        return min(self.rtts) if self.rtts else None

    @property
    def rtt_avg(self) -> float | None:
        return sum(self.rtts) / len(self.rtts) if self.rtts else None

    @property
    def rtt_max(self) -> float | None:
        return max(self.rtts) if self.rtts else None


class GuardedPingClient:
    """ICMP echo to one host, through the guard. Sends at most :data:`MAX_PING_COUNT`
    packets and finishes within ``total_timeout`` (at most :data:`MAX_PING_TOTAL_TIMEOUT`),
    resolution included."""

    def __init__(
        self,
        guard: NetGuard,
        host: str,
        *,
        count: int = 3,
        interval: float = PING_INTERVAL,
        reply_timeout: float = 1.0,
        total_timeout: float = MAX_PING_TOTAL_TIMEOUT,
        socket_factory: IcmpSocketFactory | None = None,
    ) -> None:
        if not 1 <= count <= MAX_PING_COUNT:
            raise ValueError(f"count must be between 1 and {MAX_PING_COUNT}")
        if not MIN_PING_INTERVAL <= interval <= MAX_PING_INTERVAL:
            raise ValueError(
                f"interval must be between {MIN_PING_INTERVAL} and {MAX_PING_INTERVAL} seconds"
            )
        if not 0 < reply_timeout <= MAX_PING_REPLY_TIMEOUT:
            raise ValueError(f"reply_timeout must be positive, at most {MAX_PING_REPLY_TIMEOUT}")
        if not 0 < total_timeout <= MAX_PING_TOTAL_TIMEOUT:
            raise ValueError(f"total_timeout must be positive, at most {MAX_PING_TOTAL_TIMEOUT}")
        self._guard = guard
        self._host = host
        self._count = count
        self._interval = interval
        self._reply_timeout = reply_timeout
        self._total_timeout = total_timeout
        self._socket_factory = socket_factory

    async def ping(self) -> PingResult:
        """Resolve and check the host, then send up to ``count`` echo requests.

        Raises :class:`NetGuardError` if the guard refuses the host (no socket is
        opened) and :class:`PingError` if ICMP can't be used. Lost packets are not an
        error: they show in the result. Packets the total time limit leaves no room for
        are not sent and not counted.
        """
        loop = asyncio.get_running_loop()
        started = loop.time()
        deadline = started + self._total_timeout
        # DNS gets at most PING_RESOLVE_TIMEOUT (read at call time), so a slow lookup
        # can't eat into the time the packets were promised.
        resolve_by = min(deadline, started + PING_RESOLVE_TIMEOUT)
        try:
            async with asyncio.timeout_at(resolve_by):
                address = await self._guard.resolve_for_ping(self._host)
        except TimeoutError:
            raise PingError("timed out resolving the host") from None
        # Looked up at call time, so tests can replace the module's socket layer.
        factory = self._socket_factory or open_icmp_socket
        try:
            sock = factory(address)
        except PermissionError:
            raise PingError(
                "ICMP is not permitted for this process (check the net.ipv4.ping_group_range "
                "sysctl)"
            ) from None
        except OSError:
            raise PingError(f"could not open an ICMP socket for {address}") from None
        try:
            try:
                sock.connect()
            except PermissionError:
                # SO_BROADCAST is never set, so the kernel refuses a broadcast address.
                raise PingError(
                    f"the system refused to ping {address} (is it a broadcast address?)"
                ) from None
            except OSError:
                raise PingError(f"could not reach {address} (no route?)") from None
            return await self._run(sock, address, deadline)
        finally:
            sock.close()

    async def _run(self, sock: IcmpSocket, address: IPAddress, deadline: float) -> PingResult:
        loop = asyncio.get_running_loop()
        v6 = isinstance(address, IPv6Address)
        # A fresh random nonce per run: a stale or forged reply can't be counted.
        nonce = secrets.token_bytes(_PING_NONCE_BYTES)
        identifier = secrets.randbits(16)
        sent = 0
        rtts: list[float] = []
        for sequence in range(self._count):
            started = loop.time()
            if started >= deadline:
                break
            packet = _echo_request(v6=v6, identifier=identifier, sequence=sequence, payload=nonce)
            sent += 1  # an attempt that fails to send (e.g. unreachable) counts as lost
            try:
                async with asyncio.timeout_at(deadline):
                    await sock.send(packet)
            except (OSError, TimeoutError):
                pass
            else:
                until = min(started + self._reply_timeout, deadline)
                rtt = await self._reply(sock, address, sequence, nonce, started, until)
                if rtt is not None:
                    rtts.append(rtt)
            if sequence + 1 < self._count:
                pause = min(started + self._interval, deadline) - loop.time()
                if pause > 0:
                    await asyncio.sleep(pause)
        return PingResult(address=str(address), sent=sent, received=len(rtts), rtts=tuple(rtts))

    @staticmethod
    async def _reply(
        sock: IcmpSocket,
        address: IPAddress,
        sequence: int,
        nonce: bytes,
        started: float,
        until: float,
    ) -> float | None:
        """Wait until ``until`` for this packet's reply; its round-trip time or ``None``."""
        loop = asyncio.get_running_loop()
        v6 = isinstance(address, IPv6Address)
        try:
            async with asyncio.timeout_at(until):
                while True:
                    data, source = await sock.recv()
                    if not _same_address(source, address):
                        continue  # not from the checked address
                    if _echo_reply(data, v6=v6) == (sequence, nonce):
                        return loop.time() - started
                    # Anything else (a late reply, an ICMP error, junk) is ignored.
        except (OSError, TimeoutError):
            return None
