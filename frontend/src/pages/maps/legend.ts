// What a map's legend lists: only what the cables and ports of the map actually use.

import type { MapCable, MapDocument } from "../../api";
import { SPEED_ORDER } from "./geometry";

/** What each line style means on a map. */
export const STYLE_NAME = {
  solid: "Cable",
  dotted: "Over the internet",
  dashed: "Console",
} as const;

/** Speeds, line styles, roles and VLANs that the cables and ports of this map actually use. */
export function legendEntries(doc: MapDocument) {
  const cables = doc.elements.filter((e): e is MapCable => e.kind === "cable");
  const speeds = SPEED_ORDER.filter((s) => cables.some((c) => c.speed === s));
  const used = (["solid", "dotted", "dashed"] as const).filter((s) =>
    cables.some((c) => c.style === s),
  );
  // "Cable" (solid) is only worth a row when another style needs telling apart from it.
  const styles = used.length > 1 ? used : used.filter((s) => s !== "solid");
  const roles = (["access", "trunk"] as const).filter((r) => cables.some((c) => c.role === r));
  const usedVlans = new Set<string | null>(cables.map((c) => c.vlan));
  for (const e of doc.elements) {
    if (e.kind !== "box") continue;
    for (const p of e.panel?.ports ?? []) {
      usedVlans.add(p.vlans.untagged);
      for (const t of p.vlans.tagged) usedVlans.add(t);
    }
  }
  const vlans = doc.vlans.filter((v) => usedVlans.has(v.id));
  return { speeds, styles, roles, vlans };
}
