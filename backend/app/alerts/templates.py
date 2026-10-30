"""Suggested alert rules (M7.3). Read-only: a template is only a starting point, and
creating a rule from one is an ordinary ``POST /alerts/rules``.

Metric names come from ``docs/integrations/*.md``. Scrape-only integrations (pfSense,
node_exporter hosts, smartctl_exporter, cloudflared) emit no ``vantage_instance_up``;
Prometheus' own ``up`` is their reachability signal. Every expression carries the
``vantage_integration`` label that service discovery adds, so a template never matches
another integration's series by accident.
"""

from typing import Literal

from pydantic import BaseModel

Severity = Literal["info", "warning", "critical"]


class RuleTemplate(BaseModel):
    id: str
    name: str
    description: str
    integration_types: list[str]
    expr: str
    for_seconds: int
    severity: Severity


_POLLED = ["proxmox", "snmp_device", "mikrotik_swos", "emby", "cloudflare", "http_check"]
_SCRAPED = ["pfsense", "linux_host", "smart_exporter", "cloudflared"]


def _down(type_id: str, scrape_only: bool) -> RuleTemplate:
    metric = "up" if scrape_only else "vantage_instance_up"
    return RuleTemplate(
        id=f"instance-down-{type_id}",
        name=f"{type_id}: instance down",
        description="The instance has not answered for a few minutes.",
        integration_types=[type_id],
        expr=f'{metric}{{vantage_integration="{type_id}"}} == 0',
        for_seconds=180,
        severity="critical",
    )


TEMPLATES: list[RuleTemplate] = [
    *(_down(t, False) for t in _POLLED),
    *(_down(t, True) for t in _SCRAPED),
    RuleTemplate(
        id="ping-check-down",
        name="ping_check: host not answering",
        description="Every ping packet was lost.",
        integration_types=["ping_check"],
        expr='vantage_ping_check_packet_loss_ratio{vantage_integration="ping_check"} >= 1',
        for_seconds=180,
        severity="critical",
    ),
    RuleTemplate(
        id="disk-full-linux-host",
        name="linux_host: disk above 85%",
        description="A mounted file system is more than 85% full.",
        integration_types=["linux_host"],
        expr=(
            '100 * (1 - node_filesystem_avail_bytes{vantage_integration="linux_host",'
            'fstype!~"tmpfs|overlay|squashfs"} / node_filesystem_size_bytes'
            '{vantage_integration="linux_host",fstype!~"tmpfs|overlay|squashfs"}) > 85'
        ),
        for_seconds=600,
        severity="warning",
    ),
    RuleTemplate(
        id="disk-full-pfsense",
        name="pfsense: disk above 85%",
        description="The root file system is more than 85% full (needs Telegraf's disk input).",
        integration_types=["pfsense"],
        expr='disk_used_percent{vantage_integration="pfsense",path="/"} > 85',
        for_seconds=600,
        severity="warning",
    ),
    RuleTemplate(
        id="smart-failed",
        name="smart_exporter: SMART health failed",
        description="A disk reports a failed SMART overall-health status.",
        integration_types=["smart_exporter"],
        expr='smartctl_device_smart_status{vantage_integration="smart_exporter"} == 0',
        for_seconds=60,
        severity="critical",
    ),
    RuleTemplate(
        id="temperature-smart",
        name="smart_exporter: disk temperature above 55 C",
        description="A disk runs hot.",
        integration_types=["smart_exporter"],
        expr='smartctl_device_temperature{vantage_integration="smart_exporter"} > 55',
        for_seconds=600,
        severity="warning",
    ),
    RuleTemplate(
        id="temperature-linux-host",
        name="linux_host: sensor above 80 C",
        description="A hardware temperature sensor reads above 80 C.",
        integration_types=["linux_host"],
        expr='node_hwmon_temp_celsius{vantage_integration="linux_host"} > 80',
        for_seconds=600,
        severity="warning",
    ),
    RuleTemplate(
        id="temperature-mikrotik-swos",
        name="mikrotik_swos: switch temperature above 70 C",
        description="The switch reports a high board temperature.",
        integration_types=["mikrotik_swos"],
        expr="vantage_mikrotik_swos_temperature_celsius > 70",
        for_seconds=600,
        severity="warning",
    ),
    RuleTemplate(
        id="gateway-loss-pfsense",
        name="pfsense: gateway down",
        description="A gateway lost every ping (needs Telegraf's ping input).",
        integration_types=["pfsense"],
        expr='ping_percent_packet_loss{vantage_integration="pfsense"} >= 100',
        for_seconds=120,
        severity="critical",
    ),
    *(
        RuleTemplate(
            id=f"interface-errors-{t}",
            name=f"{t}: interface errors rising",
            description="A port's receive errors keep growing.",
            integration_types=[t],
            expr=f"rate(vantage_{t}_if_in_errors_total[5m]) > 0",
            for_seconds=300,
            severity="warning",
        )
        for t in ("snmp_device", "mikrotik_swos")
    ),
    RuleTemplate(
        id="tunnel-down-cloudflared",
        name="cloudflared: no connections to the edge",
        description="The tunnel has no active connection to Cloudflare.",
        integration_types=["cloudflared"],
        expr='cloudflared_tunnel_ha_connections{vantage_integration="cloudflared"} == 0',
        for_seconds=120,
        severity="critical",
    ),
    RuleTemplate(
        id="tunnel-down-cloudflare",
        name="cloudflare: tunnel not healthy",
        description="Cloudflare reports a tunnel as not healthy.",
        integration_types=["cloudflare"],
        expr="vantage_cloudflare_tunnel_healthy == 0",
        for_seconds=300,
        severity="critical",
    ),
    RuleTemplate(
        id="raid-degraded-linux-host",
        name="linux_host: RAID degraded",
        description="An md array is degraded or has failed disks.",
        integration_types=["linux_host"],
        expr=(
            'node_md_degraded{vantage_integration="linux_host"} > 0 or '
            'node_md_disks{vantage_integration="linux_host",state="failed"} > 0'
        ),
        for_seconds=60,
        severity="critical",
    ),
]
