"""OPNsense firewall: interfaces, gateways, services and resources, polled from its REST API."""

from app.integrations.builtin.opnsense.integration import (
    Opnsense,
    OpnsenseAuthFailed,
    OpnsenseBadAnswer,
    OpnsenseConfig,
    OpnsenseNoAccess,
    OpnsensePollTooSlow,
    OpnsenseRequestFailed,
    OpnsenseTimeout,
    OpnsenseTlsUntrusted,
    OpnsenseUnreachable,
)

__all__ = [
    "Opnsense",
    "OpnsenseAuthFailed",
    "OpnsenseBadAnswer",
    "OpnsenseConfig",
    "OpnsenseNoAccess",
    "OpnsensePollTooSlow",
    "OpnsenseRequestFailed",
    "OpnsenseTimeout",
    "OpnsenseTlsUntrusted",
    "OpnsenseUnreachable",
]
