// "Read from the device" for a device whose model is known: its live physical ports go onto the
// groups of the matching preset (by interface name, else by position for a switch that reports
// ports by index), so captions, state and cables work as for any panel. Ports the preset
// doesn't place go into extra groups at the end. The result is ordinary map data.

import type { ApiClient, MapPanel, MapPort, MapPortGroup } from "../../../api";
import { scopedQuery } from "../../../cards/metrics/promql";
import { snmpMetric } from "../../../cards/snmp-overview/snmpMetrics";
import { isSfp } from "../../../cards/switch-panel/panel";
import type { DevicePort } from "../devicePorts";
import { MAX_GROUPS, MAX_PANEL_PORTS, NO_VLANS, fromDevice, newGroup, newPort } from "../panel";
import { matchPreset } from "./presets";
import type { Preset } from "./presets";

const norm = (s: string) => s.toLowerCase().replace(/\s+/g, "");
const hasInterface = (g: Pick<MapPortGroup, "kind">) => g.kind !== "console" && g.kind !== "usb";

/** The letters a list of interface names starts with in common (`port` for port1, port2). */
function commonPrefix(names: readonly string[]): string {
  const first = names[0] ?? "";
  let end = 0;
  while (end < first.length && names.every((n) => n[end] === first[end])) end++;
  return first
    .slice(0, end)
    .replace(/[\d.]+$/, "")
    .slice(0, 16);
}

export function layoutOnPreset(
  preset: Preset,
  device: readonly DevicePort[],
  existing: readonly MapPort[],
  interfaceIds: ReadonlySet<string> | "all" = new Set(),
): Pick<MapPanel, "groups" | "ports"> {
  const groups: MapPortGroup[] = preset.groups.map((g, i) => ({ ...g, id: `g${i + 1}` }));
  const slots = groups
    .filter(hasInterface)
    .flatMap((g) => Array.from({ length: g.count }, (_, i) => ({ g, n: i + 1 })));
  const key = (g: MapPortGroup, n: number) => `${g.id}\u0000${n}`;
  const placed = new Map<string, DevicePort>();

  // By name: a group with a prefix expects prefix + number (ether1, sfp-sfpplus3).
  const byName = new Map<string, { g: MapPortGroup; n: number }>();
  for (const s of slots) {
    const name = s.g.prefix ? norm(`${s.g.prefix}${s.g.first + s.n - 1}`) : "";
    if (name && !byName.has(name)) byName.set(name, s);
  }
  for (const d of device) {
    const slot = byName.get(norm(d.id)) ?? byName.get(norm(d.name));
    if (slot && !placed.has(key(slot.g, slot.n))) placed.set(key(slot.g, slot.n), d);
  }
  // A switch that names its ports by index (SwOS: port1 ... port28) goes in order instead.
  if (placed.size === 0 && device.length > 0 && device.every((d) => d.ref?.source === "snmp")) {
    device.slice(0, slots.length).forEach((d, i) => {
      const s = slots[i];
      if (s) placed.set(key(s.g, s.n), d);
    });
    for (const g of groups) {
      const mine = [...placed].filter(([k]) => k.startsWith(`${g.id}\u0000`));
      const prefix = commonPrefix(mine.map(([, d]) => d.id));
      if (prefix) g.prefix = prefix;
    }
  }

  const used = new Set(placed.values());
  const rest = device.filter((d) => !used.has(d));
  const extra = (list: readonly DevicePort[], kind: MapPortGroup["kind"]) => {
    if (list.length === 0 || groups.length >= MAX_GROUPS) return;
    const rows = kind === "rj45" && list.length > 8 ? 2 : 1;
    const g = newGroup(groups, kind, {
      count: list.length,
      rows,
      order: rows === 2 ? "odd_top" : "rows",
      prefix: commonPrefix(list.map((d) => d.id)),
    });
    groups.push(g);
    list.forEach((d, i) => placed.set(key(g, i + 1), d));
  };
  const cages = rest.filter((d) => isSfp(d));
  extra(
    rest.filter((d) => !isSfp(d)),
    "rj45",
  );
  extra(cages, cages.some((d) => (d.speed ?? 0) >= 25e9) ? "sfp28" : "sfp_plus");

  const ports: MapPort[] = [];
  for (const g of groups) {
    for (let n = 1; n <= g.count; n++) {
      const d = placed.get(key(g, n));
      const prev = d && existing.find((p) => p.interface === d.id || p.id === d.id);
      const named = d && (interfaceIds === "all" || interfaceIds.has(d.id));
      ports.push(
        newPort(prev?.id ?? (d && named ? d.id : `${g.id}-${n}`), {
          group: g.id,
          n,
          interface: d?.id ?? "",
          caption: prev?.caption ?? "",
          vlans: prev?.vlans ?? NO_VLANS,
          hidden: prev?.hidden ?? false,
        }),
      );
    }
  }
  return { groups, ports: ports.slice(0, MAX_PANEL_PORTS) };
}

/**
 * What "Read from the device" draws: the matching preset's layout when the device's sysDescr
 * names a known model, otherwise the plain read of its interfaces.
 */
export function readDevicePanel(
  sysDescr: string | undefined,
  device: readonly DevicePort[],
  existing: readonly MapPort[],
  interfaceIds: ReadonlySet<string> | "all" = new Set(),
): Pick<MapPanel, "groups" | "ports"> {
  const known = matchPreset(sysDescr);
  return known
    ? layoutOnPreset(known, device, existing, interfaceIds)
    : fromDevice(device, existing, interfaceIds);
}

/** The device's sysDescr from its `*_info` series; undefined when unknown or unreachable. */
export async function fetchSysDescr(
  client: Pick<ApiClient, "query">,
  instanceId: string,
): Promise<string | undefined> {
  try {
    const result = await client.query({
      query: scopedQuery(instanceId, (sel) => snmpMetric(sel, "info")),
    });
    return result.series[0]?.labels.sys_descr || undefined;
  } catch {
    return undefined;
  }
}
