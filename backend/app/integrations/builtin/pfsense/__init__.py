"""pfSense firewall via the Telegraf package's prometheus_client output."""

from app.integrations.builtin.pfsense.integration import PfSense, PfSenseConfig

__all__ = ["PfSense", "PfSenseConfig"]
