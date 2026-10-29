"""A fake network for integration tests: no sockets, but the real SSRF guard.

Integrations get their HTTP client from ``ctx.http_client()``, which builds a guarded
client (``net_guard.guarded_http_client``). The ``fake_net`` fixture keeps that whole
path, including the guard's DNS resolution and address checks, and swaps only the
socket layer underneath for :class:`FakeNetwork`. So a test proves both what the
integration does with a response *and* that its request went through the guard to the
checked address.

Every request is recorded, so a test can assert which methods and paths an integration
used (the "no write endpoints called" check in AGENTS.md).

Addresses: routes are keyed by the *checked* address the guard connects to. DNS names
resolve through :data:`DNS`; anything else is unknown.

ICMP works the same way: ``fake_icmp`` keeps the real guard and ping client and swaps
only the ICMP socket opener for :class:`FakeIcmp`.
"""

from __future__ import annotations

import asyncio
import ssl
import struct
import uuid
from collections.abc import Awaitable, Callable, Iterator, Sequence
from dataclasses import dataclass, field
from ipaddress import IPv6Address
from typing import Any

import httpcore
import httpx
import pytest
from pydantic import BaseModel

from app.core import net_guard
from app.core.net_guard import IPAddress, NetGuard, NetPolicy
from app.integrations import base
from app.integrations.base import InstanceContext, Integration

# Private names resolve to private addresses; one name points at the metadata address.
DNS: dict[str, list[str]] = {
    "web.example.internal": ["10.0.0.10"],
    "node.example.internal": ["10.0.0.20"],
    "ping.example.internal": ["10.0.0.30"],
    "fw.example.internal": ["10.0.0.1"],
    "snmp.example.internal": ["10.0.0.40"],
    "metadata.example.internal": ["169.254.169.254"],
}


async def fake_resolver(host: str, port: int) -> Sequence[str]:
    if host not in DNS:
        raise OSError("no such host")
    return DNS[host]


def fake_guards(cls: type[Integration]) -> NetGuard:
    """A guard with the default policy, the fake resolver and no "own addresses"."""
    return NetGuard(
        NetPolicy(own_addresses=frozenset()), cloud_hosts=cls.cloud_hosts, resolver=fake_resolver
    )


@dataclass(frozen=True)
class FakeRequest:
    address: str  # where the guard connected
    port: int
    method: str
    target: str  # path and query
    headers: dict[str, str]  # lowercased names


Handler = Callable[[FakeRequest], Awaitable[bytes] | bytes]


def http_response(
    status: int = 200,
    body: bytes = b"",
    *,
    reason: str = "OK",
    headers: Sequence[tuple[str, str]] = (),
) -> bytes:
    lines = [f"HTTP/1.1 {status} {reason}", f"Content-Length: {len(body)}"]
    lines += [f"{k}: {v}" for k, v in headers]
    return ("\r\n".join(lines) + "\r\n\r\n").encode() + body


def _parse(raw: bytes, address: str, port: int) -> FakeRequest:
    head = raw.split(b"\r\n\r\n", 1)[0].decode("latin-1")
    request_line, *header_lines = head.split("\r\n")
    method, target, _ = request_line.split(" ", 2)
    headers = {}
    for line in header_lines:
        name, _, value = line.partition(":")
        headers[name.strip().lower()] = value.strip()
    return FakeRequest(address, port, method, target, headers)


class _Stream(httpcore.AsyncNetworkStream):
    def __init__(self, network: FakeNetwork, address: str, port: int, handler: Handler) -> None:
        self._network = network
        self._address = address
        self._port = port
        self._handler = handler
        self._written = b""
        self._response: bytes | None = None

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:  # noqa: ASYNC109
        self._written += buffer

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:  # noqa: ASYNC109
        if self._response is None:
            request = _parse(self._written, self._address, self._port)
            self._network.requests.append(request)
            result = self._handler(request)
            self._response = result if isinstance(result, bytes) else await result
        chunk, self._response = self._response[:max_bytes], self._response[max_bytes:]
        return chunk

    async def aclose(self) -> None:
        pass

    async def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,  # noqa: ASYNC109
    ) -> httpcore.AsyncNetworkStream:
        self._network.tls.append((server_hostname, ssl_context))
        if self._network.tls_error is not None:
            raise self._network.tls_error
        return self

    def get_extra_info(self, info: str) -> Any:
        return None


@dataclass
class FakeNetwork(httpcore.AsyncNetworkBackend):
    """The socket layer. ``routes[(address, port)]`` answers requests to that address;
    an exception there is raised on connect; an unknown address is "refused"."""

    routes: dict[tuple[str, int], Handler | BaseException] = field(default_factory=dict)
    requests: list[FakeRequest] = field(default_factory=list)
    connects: list[tuple[str, int]] = field(default_factory=list)
    tls: list[tuple[str | None, ssl.SSLContext]] = field(default_factory=list)
    tls_error: BaseException | None = None
    # The guarded client's total time limit; lower it to test its timeout quickly.
    total_timeout: float = net_guard.DEFAULT_TOTAL_TIMEOUT

    def serve(self, address: str, port: int, handler: Handler | bytes) -> None:
        if isinstance(handler, bytes):
            payload = handler
            self.routes[(address, port)] = lambda _request: payload
        else:
            self.routes[(address, port)] = handler

    def fail(self, address: str, port: int, error: BaseException) -> None:
        self.routes[(address, port)] = error

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        self.connects.append((host, port))
        route = self.routes.get((host, port))
        if route is None:
            raise httpcore.ConnectError("connection refused")
        if isinstance(route, BaseException):
            raise route
        return _Stream(self, host, port, route)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


async def hang(_request: FakeRequest) -> bytes:
    """A handler that never answers (until the caller's timeout)."""
    await asyncio.sleep(3600)
    raise AssertionError("unreachable")


@pytest.fixture
def fake_net(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeNetwork]:
    """Route every ``ctx.http_client()`` through the real guard onto a FakeNetwork."""
    network = FakeNetwork()
    real = net_guard.guarded_http_client

    def patched(guard: NetGuard, **kwargs: Any) -> httpx.AsyncClient:
        return real(guard, network_backend=network, total_timeout=network.total_timeout, **kwargs)

    monkeypatch.setattr(base, "guarded_http_client", patched)
    yield network


# --- ICMP ------------------------------------------------------------------------------
#
# ``ctx.ping_client()`` builds a real GuardedPingClient: the guard resolves and checks the
# host, then the client opens an ICMP socket to the checked address through
# ``net_guard.open_icmp_socket``. ``fake_icmp`` replaces only that opener.

ECHO_REQUEST = {False: 8, True: 128}
ECHO_REPLY = {False: 0, True: 129}
_HEADER = struct.Struct("!BBHHH")

# Per echo request (by sequence number): seconds until the reply, or None to drop it.
Responder = Callable[[int], float | None]


def always(delay: float = 0.002) -> Responder:
    return lambda _sequence: delay


def never(_sequence: int) -> float | None:
    return None


def drop(*sequences: int, delay: float = 0.002) -> Responder:
    return lambda sequence: None if sequence in sequences else delay


@dataclass(frozen=True)
class SentPing:
    address: str  # the address the socket was opened to (the checked one)
    type: int
    code: int
    identifier: int
    sequence: int
    payload: bytes
    raw: bytes


def reply_for(request: SentPing, *, v6: bool, payload: bytes | None = None) -> bytes:
    body = request.payload if payload is None else payload
    return _HEADER.pack(ECHO_REPLY[v6], 0, 0, request.identifier, request.sequence) + body


Inbound = bytes | tuple[bytes, str] | BaseException


class FakeIcmpSocket:
    def __init__(self, network: FakeIcmp, address: IPAddress) -> None:
        self._network = network
        self.address = address
        self.v6 = isinstance(address, IPv6Address)
        self._inbox: asyncio.Queue[Inbound] = asyncio.Queue()
        self.connected = False
        self.closed = False
        for item in network.preload:
            self._inbox.put_nowait(item)

    def connect(self) -> None:
        if self._network.connect_error is not None:
            raise self._network.connect_error
        self.connected = True

    async def send(self, packet: bytes) -> None:
        assert self.connected and not self.closed
        kind, code, _, identifier, sequence = _HEADER.unpack_from(packet)
        sent = SentPing(
            str(self.address), kind, code, identifier, sequence, packet[_HEADER.size :], packet
        )
        self._network.sent.append(sent)
        if self._network.send_error is not None:
            raise self._network.send_error
        responder = self._network.hosts.get(str(self.address), never)
        delay = responder(sequence)
        if delay is not None and kind == ECHO_REQUEST[self.v6]:
            source = self._network.reply_source or str(self.address)
            message = (reply_for(sent, v6=self.v6), source)
            asyncio.get_running_loop().call_later(delay, self._inbox.put_nowait, message)

    async def recv(self) -> tuple[bytes, str]:
        item = await self._inbox.get()
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, bytes):
            return item, str(self.address)  # preloaded junk "from" the checked address
        return item

    def close(self) -> None:
        self.closed = True


@dataclass
class FakeIcmp:
    """``hosts[address]`` decides how each echo request to that checked address is answered;
    an address not listed never answers."""

    hosts: dict[str, Responder] = field(default_factory=dict)
    opened: list[FakeIcmpSocket] = field(default_factory=list)
    sent: list[SentPing] = field(default_factory=list)
    open_error: BaseException | None = None
    connect_error: BaseException | None = None
    send_error: BaseException | None = None
    # Answer from this source address instead of the pinged one (a spoofed reply).
    reply_source: str | None = None
    # Messages every new socket receives first (junk, stale replies, errors); bare bytes
    # come "from" the checked address, (bytes, source) pairs from that source.
    preload: list[Inbound] = field(default_factory=list)

    def answer(self, address: str, responder: Responder | None = None) -> None:
        self.hosts[address] = responder or always()

    def open(self, address: IPAddress) -> FakeIcmpSocket:
        if self.open_error is not None:
            raise self.open_error
        sock = FakeIcmpSocket(self, address)
        self.opened.append(sock)
        return sock

    @property
    def addresses(self) -> list[str]:
        return [str(s.address) for s in self.opened]


@pytest.fixture
def fake_icmp(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeIcmp]:
    """Every guarded ping goes through the real guard onto a FakeIcmp socket layer."""
    network = FakeIcmp()
    monkeypatch.setattr(net_guard, "open_icmp_socket", network.open)
    yield network


def make_ctx(cls: type[Integration], config: dict[str, Any]) -> InstanceContext:
    """A context like the instance API builds for a connection test."""
    model: BaseModel = cls.config_schema.model_validate(config)
    return InstanceContext(
        instance_id=uuid.uuid4(), name="test-instance", config=model, net_guard=fake_guards(cls)
    )
