"""Fixtures for the built-in integration tests."""

from tests.integrations.netfake import fake_icmp, fake_net
from tests.integrations.snmpfake import fake_snmp

__all__ = ["fake_icmp", "fake_net", "fake_snmp"]
