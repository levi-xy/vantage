"""cloudflared tunnel connector via its local metrics endpoint."""

from app.integrations.builtin.cloudflared.integration import Cloudflared, CloudflaredConfig

__all__ = ["Cloudflared", "CloudflaredConfig"]
