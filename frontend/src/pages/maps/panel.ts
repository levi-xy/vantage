// A box's front panel (document `panel`, M11.1): what the old port strip looked like,
// read from the new group/port shape. The new jacks are drawn from M11.3 on.

import type { MapPanel, MapPort, MapPortKind } from "../../api";

/** Cages (any SFP or QSFP) are drawn wider than RJ45 jacks; console and USB look like RJ45. */
export function isCage(kind: MapPortKind | undefined): boolean {
  return kind === "sfp" || kind === "sfp_plus" || kind === "sfp28" || kind === "qsfp";
}

/** How a port is drawn: a cage or a jack, from its group's kind. */
export function portKind(panel: MapPanel, port: MapPort): "rj45" | "sfp" {
  return isCage(panel.groups.find((g) => g.id === port.group)?.kind) ? "sfp" : "rj45";
}

/** The single-row or two-row layout the strip shows today. */
export function panelLayout(panel: MapPanel): "row" | "panel" {
  return panel.groups.some((g) => g.rows === 2) ? "panel" : "row";
}

export const NO_VLANS: MapPort["vlans"] = { untagged: null, tagged: [] };

export function emptyPanel(source: MapPanel["source"]): MapPanel {
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

/**
 * Typed ports, in order: each goes into the group of its kind (made on demand; `kinds`
 * overrides the kind of a port that is new or changed), then the groups agree with the ports:
 * counts follow the ports, numbers follow the order, empty groups go.
 */
export function withTypedPorts(
  panel: MapPanel,
  ports: readonly MapPort[],
  kinds: ReadonlyMap<string, "rj45" | "sfp"> = new Map(),
): MapPanel {
  const rows = panelLayout(panel) === "panel" ? 2 : 1;
  const groups = [...panel.groups];
  const groupFor = (kind: "rj45" | "sfp"): string => {
    const found = groups.find((g) => (kind === "sfp" ? isCage(g.kind) : g.kind === "rj45"));
    if (found) return found.id;
    const id = `g-${kind}`;
    groups.push({ id, kind, count: 1, rows, odd_top: false, first: 1, prefix: "" });
    return id;
  };
  const counts = new Map<string, number>();
  const placed = ports.map((p) => {
    const kind = kinds.get(p.id);
    const group =
      !kind && groups.some((g) => g.id === p.group) ? p.group : groupFor(kind ?? "rj45");
    const n = (counts.get(group) ?? 0) + 1;
    counts.set(group, n);
    return { ...p, group, n };
  });
  return {
    ...panel,
    groups: groups
      .filter((g) => counts.has(g.id))
      .map((g) => ({ ...g, count: counts.get(g.id) ?? 1 })),
    ports: placed,
  };
}

/** The same panel with every group on one row or two. */
export function withLayout(panel: MapPanel, layout: "row" | "panel"): MapPanel {
  const rows = layout === "panel" ? 2 : 1;
  return { ...panel, groups: panel.groups.map((g) => ({ ...g, rows })) };
}
