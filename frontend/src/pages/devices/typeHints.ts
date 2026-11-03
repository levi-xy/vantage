// One line per integration type for the "Add device" type picker: what it watches and how
// it gets there. A type missing here just shows no hint.
export const TYPE_HINTS: Readonly<Record<string, string>> = {
  ping_check: "Is a host answering ping? Round trip and loss.",
  http_check: "Is a web page or API answering? Status code and response time.",
  linux_host: "CPU, memory, disks and network of a Linux machine running node_exporter.",
  smart_exporter: "Disk health (SMART) from smartctl_exporter.",
  snmp_device: "Any SNMP device: uptime and interface traffic.",
  mikrotik_swos: "MikroTik switch running SwOS, over SNMP: ports and traffic.",
  pfsense: "pfSense firewall through Telegraf: interfaces, gateways, resources.",
  proxmox: "Proxmox VE nodes, VMs and containers, through the API with a read-only token.",
  cloudflared: "A Cloudflare Tunnel connector, from its metrics endpoint.",
  cloudflare: "A Cloudflare account: zone traffic and tunnels, with a read-only API token.",
  emby: "An Emby media server: sessions and libraries.",
};

/** Does a type match the search text? Name, type id, category and hint, any case. */
export function matchesType(
  type: { type_id: string; name: string; category: string },
  search: string,
): boolean {
  const words = search.toLowerCase().split(/\s+/).filter(Boolean);
  const text = [type.name, type.type_id, type.category, TYPE_HINTS[type.type_id] ?? ""]
    .join(" ")
    .toLowerCase();
  return words.every((w) => text.includes(w));
}
