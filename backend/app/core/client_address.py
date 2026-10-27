"""The address of the client behind a request, honouring trusted reverse proxies (M1.7).

Trust is decided only by the direct TCP peer. When the peer is in
``VANTAGE_TRUSTED_PROXIES``, the client address is read from the single
``X-Real-IP`` header, which the bundled nginx *overwrites* with ``$remote_addr``.
``X-Forwarded-For`` is never used: clients can prepend anything to it.

Every part of the app that needs the client address uses :func:`client_address`,
so the login limiter and anything else that records an address agree.
"""

from collections.abc import Iterable, Sequence
from ipaddress import IPv4Address, IPv4Network, IPv6Address, IPv6Network, ip_address, ip_network

IPNetwork = IPv4Network | IPv6Network
IPAddress = IPv4Address | IPv6Address

# The one header read from a trusted proxy. nginx sets it with
# `proxy_set_header X-Real-IP $remote_addr;`, which replaces any value the client sent.
CLIENT_ADDRESS_HEADER = "x-real-ip"

# Used when the server gives no peer address at all (not seen with uvicorn over TCP).
UNKNOWN_CLIENT = "unknown"


class TrustedProxiesError(ValueError):
    """``VANTAGE_TRUSTED_PROXIES`` holds something that isn't a CIDR or an address."""


def parse_trusted_proxies(raw: str, name: str = "VANTAGE_TRUSTED_PROXIES") -> tuple[IPNetwork, ...]:
    """Parse a comma- or whitespace-separated list of CIDRs (a bare address is a /32 or /128).

    Empty means "trust nobody". Host bits set (``10.0.0.5/24``) are refused, since that's
    usually a typo for a narrower range.
    """
    networks: list[IPNetwork] = []
    for position, entry in enumerate(raw.replace(",", " ").split(), start=1):
        try:
            network = ip_network(entry, strict=True)
        except ValueError:
            raise TrustedProxiesError(
                f"{name} entry {position} is not a valid CIDR or address "
                "(or has host bits set); use e.g. 172.16.238.2/32"
            ) from None
        if isinstance(network, IPv6Network) and network.network_address.ipv4_mapped is not None:
            # Peers are compared in their IPv4 form, so this entry would never match.
            raise TrustedProxiesError(
                f"{name} entry {position}: write IPv4-mapped addresses as IPv4"
            )
        networks.append(network)
    return tuple(networks)


def _parse_address(value: str) -> IPAddress | None:
    """A valid IP address in one canonical form.

    IPv4-mapped IPv6 (``::ffff:192.0.2.1``) becomes IPv4, and an IPv6 zone
    (``fe80::1%eth0``) is dropped, so one client can't show up under many spellings.
    """
    try:
        address = ip_address(value.strip())
    except ValueError:
        return None
    if isinstance(address, IPv6Address):
        mapped = address.ipv4_mapped
        return mapped if mapped is not None else IPv6Address(int(address))
    return address


def _is_trusted(peer: IPAddress, trusted: Iterable[IPNetwork]) -> bool:
    return any(peer.version == net.version and peer in net for net in trusted)


def resolve_client_address(
    peer: str | None,
    header_values: Sequence[str],
    trusted: Sequence[IPNetwork],
) -> str:
    """The client address for a request whose direct TCP peer is ``peer``.

    ``header_values`` are every ``X-Real-IP`` value on the request. They are used only
    when the peer is a trusted proxy and there is exactly one valid IP address among
    them (one header, one value); anything else falls back to the peer. Addresses are
    returned in canonical text form, IPv4-mapped IPv6 as plain IPv4.
    """
    if peer is None:
        return UNKNOWN_CLIENT
    peer_address = _parse_address(peer)
    if peer_address is None:
        # Not an IP (e.g. a Unix socket path, or the test client): never a trusted proxy.
        return peer
    if trusted and _is_trusted(peer_address, trusted) and len(header_values) == 1:
        forwarded = _parse_address(header_values[0])
        if forwarded is not None:
            return str(forwarded)
    return str(peer_address)
