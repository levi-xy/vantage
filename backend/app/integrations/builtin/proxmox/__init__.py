"""Proxmox VE: nodes, guests and storage pools, polled from the REST API with a token."""

from app.integrations.builtin.proxmox.integration import (
    Proxmox,
    ProxmoxAuthFailed,
    ProxmoxBadAnswer,
    ProxmoxConfig,
    ProxmoxForbidden,
    ProxmoxPollTooSlow,
    ProxmoxRequestFailed,
    ProxmoxTimeout,
    ProxmoxTlsUntrusted,
    ProxmoxUnreachable,
)

__all__ = [
    "Proxmox",
    "ProxmoxAuthFailed",
    "ProxmoxBadAnswer",
    "ProxmoxConfig",
    "ProxmoxForbidden",
    "ProxmoxPollTooSlow",
    "ProxmoxRequestFailed",
    "ProxmoxTimeout",
    "ProxmoxTlsUntrusted",
    "ProxmoxUnreachable",
]
