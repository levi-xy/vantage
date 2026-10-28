"""The compose network layout that VANTAGE_TRUSTED_PROXIES depends on (M1.7).

The backend trusts exactly the frontend's fixed address. That only holds if the frontend
address can't be handed to another container (it is outside ``ip_range``) and the
gateway, which host-loopback clients appear to come from, is pinned to a known address
that is neither the frontend nor inside ``ip_range``.
"""

from __future__ import annotations

import re
from ipaddress import IPv4Address, IPv4Network, ip_address, ip_network
from pathlib import Path
from typing import Any

import pytest
import yaml

COMPOSE = Path(__file__).resolve().parents[2] / "deploy" / "docker-compose.yml"


@pytest.fixture(scope="module")
def compose() -> dict[str, Any]:
    with COMPOSE.open(encoding="utf-8") as f:
        data: dict[str, Any] = yaml.safe_load(f)
    return data


@pytest.fixture(scope="module")
def ipam(compose: dict[str, Any]) -> dict[str, str]:
    configs = compose["networks"]["default"]["ipam"]["config"]
    assert len(configs) == 1, "one IPv4 pool expected"
    config: dict[str, str] = configs[0]
    return config


def _frontend_address(compose: dict[str, Any]) -> IPv4Address:
    addr = ip_address(compose["services"]["frontend"]["networks"]["default"]["ipv4_address"])
    assert isinstance(addr, IPv4Address)
    return addr


def _net(value: str) -> IPv4Network:
    net = ip_network(value)  # strict: host bits set would raise
    assert isinstance(net, IPv4Network)
    return net


def test_gateway_is_pinned_inside_subnet_and_outside_ip_range(
    compose: dict[str, Any], ipam: dict[str, str]
) -> None:
    subnet, ip_range = _net(ipam["subnet"]), _net(ipam["ip_range"])
    assert "gateway" in ipam, "unpinned, Docker 29 picks the gateway from ip_range"
    gateway = ip_address(ipam["gateway"])
    assert gateway in subnet
    assert gateway not in ip_range
    assert gateway not in (subnet.network_address, subnet.broadcast_address)
    assert gateway != _frontend_address(compose)


def test_frontend_address_is_reserved(compose: dict[str, Any], ipam: dict[str, str]) -> None:
    subnet, ip_range = _net(ipam["subnet"]), _net(ipam["ip_range"])
    assert ip_range.subnet_of(subnet)
    frontend = _frontend_address(compose)
    assert frontend in subnet
    assert frontend not in ip_range  # no other container can be given it
    assert frontend not in (subnet.network_address, subnet.broadcast_address)


def test_trusted_proxies_default_is_exactly_the_frontend(compose: dict[str, Any]) -> None:
    value = compose["services"]["backend"]["environment"]["VANTAGE_TRUSTED_PROXIES"]
    match = re.fullmatch(r"\$\{VANTAGE_TRUSTED_PROXIES:-([^}]*)\}", value)
    assert match, "the compose default must stay overridable from .env"
    assert ip_network(match.group(1)) == ip_network(f"{_frontend_address(compose)}/32")
