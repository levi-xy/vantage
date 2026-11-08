// A box's front panel (document `panel`, M11): port groups, the ports they make, and the few
// pure operations the editor and the drawing share. Everything here is data in the map: no
// device or vendor is known to the code (the model list is presets, in editor/presets.ts).

import type { MapPanel, MapPort, MapPortGroup, MapPortKind } from "../../api";
import { isSfp } from "../../cards/switch-panel/panel";
import type { DevicePort } from "./devicePorts";

export const MAX_GROUPS = 8;
export const MAX_PANEL_PORTS = 128;
export const MAX_GROUP_COUNT = 64;

export const KIND_LABEL: Record<MapPortKind, string> = {
  rj45: "RJ45",
  sfp: "SFP",
  sfp_plus: "SFP+",
  sfp28: "SFP28",
  qsfp: "QSFP",
  console: "Console",
  usb: "USB",
};

/** Cages (any SFP or QSFP) are drawn wider than RJ45 jacks. */
export function isCage(kind: MapPortKind | undefined): boolean {
  return kind === "sfp" || kind === "sfp_plus" || kind === "sfp28" || kind === "qsfp";
}

export const NO_VLANS: MapPort["vlans"] = { untagged: null, tagged: [] };

export function emptyPanel(source: MapPanel["source"] = "manual"): MapPanel {
  return {
    look: "flat",
    place: "bottom",
    size: "m",
    numbers: true,
    captions: "typed",
    source,
    groups: [],
    ports: [],
  };
}

/** A port with defaults for everything but its id. */
export function newPort(id: string, patch: Partial<MapPort> = {}): MapPort {
  return {
    id,
    group: "",
    n: 0,
    caption: "",
    interface: "",
    vlans: NO_VLANS,
    hidden: false,
    ...patch,
  };
}

/** The number a port shows: its group's first number plus its place in the group. */
export const portNumber = (group: MapPortGroup, port: MapPort): number => group.first + port.n - 1;

/** The lowest unused group id (`g1`, `g2`, ...). */
export function newGroupId(groups: readonly { id: string }[]): string {
  const used = new Set(groups.map((g) => g.id));
  for (let n = 1; ; n++) if (!used.has(`g${n}`)) return `g${n}`;
}

export function newGroup(
  groups: readonly MapPortGroup[],
  kind: MapPortKind,
  patch: Partial<MapPortGroup> = {},
): MapPortGroup {
  return {
    id: newGroupId(groups),
    kind,
    count: kind === "console" || kind === "usb" ? 1 : 4,
    rows: 1,
    order: "rows",
    first: 1,
    prefix: "",
    ...patch,
  };
}

/**
 * The ports the groups describe. A port of a number that still exists keeps its id, caption,
 * VLANs and interface (so cables stay attached); new numbers get `<group>-<n>`; ports of
 * numbers or groups that are gone drop out.
 */
export function regeneratePorts(panel: MapPanel): MapPanel {
  const old = new Map(panel.ports.map((p) => [`${p.group}\u0000${p.n}`, p] as const));
  const ports: MapPort[] = [];
  for (const g of panel.groups) {
    for (let n = 1; n <= g.count; n++) {
      const prev = old.get(`${g.id}\u0000${n}`);
      ports.push(
        prev ??
          newPort(`${g.id}-${n}`, {
            group: g.id,
            n,
            interface: g.prefix ? `${g.prefix}${g.first + n - 1}` : "",
          }),
      );
    }
  }
  return { ...panel, ports: ports.slice(0, MAX_PANEL_PORTS) };
}

/** The letters a list of interface names starts with in common (`ether` for ether1, ether2). */
function commonPrefix(names: readonly string[]): string {
  const first = names[0] ?? "";
  let end = 0;
  while (end < first.length && names.every((n) => n[end] === first[end])) end++;
  return first
    .slice(0, end)
    .replace(/[\d.]+$/, "")
    .slice(0, 16);
}

/**
 * Groups and ports for a device's own interfaces: copper in one group (two rows, odd numbers
 * on top, past 8 ports), cages in one group of their own. Existing ports that name an
 * interface keep their id, caption and VLANs; `interfaceIds` makes new ports take the
 * interface name as their id (what a cable to an unlisted device port already holds).
 */
export function fromDevice(
  device: readonly DevicePort[],
  existing: readonly MapPort[],
  interfaceIds: ReadonlySet<string> | "all" = new Set(),
): Pick<MapPanel, "groups" | "ports"> {
  const cages = device.filter((d) => isSfp(d));
  const copper = device.filter((d) => !isSfp(d));
  const groups: MapPortGroup[] = [];
  const ports: MapPort[] = [];
  const add = (list: readonly DevicePort[], kind: MapPortKind, rows: 1 | 2) => {
    if (list.length === 0) return;
    const group = newGroup(groups, kind, {
      count: list.length,
      rows,
      order: rows === 2 ? "odd_top" : "rows",
      prefix: commonPrefix(list.map((d) => d.id)),
    });
    groups.push(group);
    list.forEach((d, i) => {
      const prev = existing.find((p) => p.interface === d.id || p.id === d.id);
      const byName = interfaceIds === "all" || interfaceIds.has(d.id);
      ports.push(
        newPort(prev?.id ?? (byName ? d.id : `${group.id}-${i + 1}`), {
          group: group.id,
          n: i + 1,
          interface: d.id,
          caption: prev?.caption ?? "",
          vlans: prev?.vlans ?? NO_VLANS,
          hidden: prev?.hidden ?? false,
        }),
      );
    });
  };
  add(copper, "rj45", copper.length > 8 ? 2 : 1);
  add(cages, cages.some((d) => (d.speed ?? 0) >= 25e9) ? "sfp28" : "sfp_plus", 1);
  return { groups, ports: ports.slice(0, MAX_PANEL_PORTS) };
}

/**
 * What the drawing shows: the document's groups and ports. A panel read from the device
 * that has no ports of its own yet (an older map) shows the device's interfaces, with the
 * interface name as the port id (what its cables hold).
 */
export function resolvePanel(
  panel: MapPanel,
  device: readonly DevicePort[] | undefined,
): Pick<MapPanel, "groups" | "ports"> {
  if (panel.source === "device" && panel.ports.length === 0 && device && device.length > 0) {
    return fromDevice(device, [], "all");
  }
  return { groups: panel.groups, ports: panel.ports };
}

/** The name a port shows: from the cable's other end, else the typed caption. */
export function portCaption(
  captions: "cables" | "typed" | "off",
  typed: string,
  fromCable: string | undefined,
): string {
  if (captions === "off") return "";
  return captions === "cables" ? (fromCable ?? typed) : typed;
}
