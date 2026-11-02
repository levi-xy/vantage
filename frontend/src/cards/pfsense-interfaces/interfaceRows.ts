// Row order for the pfSense Interfaces card: WAN pinned first, then busiest first (in + out),
// VLAN sub-interfaces (`igc1.10`) indented under their parent when the parent is shown.

export interface InterfaceRate {
  /** The OS name, as Telegraf reports it. */
  id: string;
  /** The display name (`interface_names`), or the OS name. */
  name: string;
  rx: number | undefined;
  tx: number | undefined;
}

export interface InterfaceRow extends InterfaceRate {
  total: number;
  /** A VLAN sub-interface shown under its parent. */
  child: boolean;
  /** The WAN interface, pinned to the top. */
  pinned: boolean;
}

const VLAN = /^(.+)\.(\d+)$/;
const total = (r: InterfaceRate) => (r.rx ?? 0) + (r.tx ?? 0);
const isWan = (r: InterfaceRate) => r.name.toLowerCase() === "wan" || r.id.toLowerCase() === "wan";

const busiestFirst = (a: InterfaceRow, b: InterfaceRow) =>
  b.total - a.total || a.name.localeCompare(b.name);

/**
 * Telegraf doesn't say which interface holds the default route, so WAN is the interface
 * named (or mapped to) "WAN". Without one, nothing is pinned.
 */
export function interfaceRows(rates: InterfaceRate[]): InterfaceRow[] {
  const ids = new Set(rates.map((r) => r.id));
  const wan = rates.find(isWan)?.id;
  const children = new Map<string, InterfaceRow[]>();
  const top: InterfaceRow[] = [];
  for (const r of rates) {
    const parent = VLAN.exec(r.id)?.[1];
    const row = { ...r, total: total(r), child: false, pinned: r.id === wan };
    if (parent !== undefined && parent !== r.id && ids.has(parent) && r.id !== wan) {
      row.child = true;
      children.set(parent, [...(children.get(parent) ?? []), row]);
    } else {
      top.push(row);
    }
  }
  top.sort((a, b) => Number(b.pinned) - Number(a.pinned) || busiestFirst(a, b));
  const withChildren = (r: InterfaceRow): InterfaceRow[] => [
    r,
    ...(children.get(r.id) ?? []).sort(busiestFirst).flatMap(withChildren),
  ];
  return top.flatMap(withChildren);
}
