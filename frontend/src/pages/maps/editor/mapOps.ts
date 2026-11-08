// Pure document operations for cables, port strips and VLANs (M9.4). Every one returns a
// document the backend accepts: cable ends exist and differ, typed port ids exist, VLAN ids
// exist, caps respected. Nothing here touches React or the network.

import type { MapCable, MapCableEnd, MapDocument, MapPanel, MapSpeed, MapVlan } from "../../../api";
import { DEVICE_COLORS } from "../../../app/deviceColors";
import { clamp, MAX_COORD, newId, patchElement, snapTo } from "./model";

export const MAX_BENDS = 16;
export const MAX_VLANS = 64;
export const MAX_PORTS = 64;
export const MAX_TAGGED = 16;
export const MAX_GUEST_LIST = 200;

type Bend = [number, number];

// --- Cables ---------------------------------------------------------------------------

const SPEED_BPS: [number, MapSpeed][] = [
  [100e6, "100M"],
  [1e9, "1G"],
  [2.5e9, "2.5G"],
  [5e9, "5G"],
  [10e9, "10G"],
  [25e9, "25G"],
  [40e9, "40G"],
  [100e9, "100G"],
];

/** The cable speed for a port speed in bits per second, or null when it is none of ours. */
export function speedFromBps(bps: number | undefined): MapSpeed | null {
  if (bps === undefined) return null;
  return SPEED_BPS.find(([v]) => Math.abs(v - bps) <= v * 0.01)?.[1] ?? null;
}

/** A new cable; null for a self-connection (never allowed). */
export function makeCable(
  doc: MapDocument,
  from: MapCableEnd,
  to: MapCableEnd,
  speed: MapSpeed | null,
): MapCable | null {
  if (from.element === to.element) return null;
  return {
    id: newId(doc, "c"),
    kind: "cable",
    from,
    to,
    speed,
    style: "solid",
    role: null,
    vlan: null,
    color: null,
    label: "",
    show_rate: false,
    bends: [],
  };
}

const snapPoint = (x: number, y: number): Bend => [
  clamp(snapTo(x), 0, MAX_COORD),
  clamp(snapTo(y), 0, MAX_COORD),
];

/** A new bend at `index`, snapped to the grid; nothing happens at the cap. */
export function insertBend(bends: readonly Bend[], index: number, x: number, y: number): Bend[] {
  const next = bends.map((b): Bend => [b[0], b[1]]);
  if (next.length < MAX_BENDS) next.splice(index, 0, snapPoint(x, y));
  return next;
}

export function moveBend(bends: readonly Bend[], index: number, x: number, y: number): Bend[] {
  return bends.map((b, i): Bend => (i === index ? snapPoint(x, y) : [b[0], b[1]]));
}

export function removeBend(bends: readonly Bend[], index: number): Bend[] {
  return bends.filter((_, i) => i !== index).map((b): Bend => [b[0], b[1]]);
}

/** Squared distance from (px, py) to the segment a-b. */
function segDist(px: number, py: number, a: readonly number[], b: readonly number[]): number {
  const [ax, ay, bx, by] = [a[0] ?? 0, a[1] ?? 0, b[0] ?? 0, b[1] ?? 0];
  const len = (bx - ax) ** 2 + (by - ay) ** 2;
  const t = len === 0 ? 0 : clamp(((px - ax) * (bx - ax) + (py - ay) * (by - ay)) / len, 0, 1);
  return (px - (ax + t * (bx - ax))) ** 2 + (py - (ay + t * (by - ay))) ** 2;
}

/**
 * Where a new bend dragged out of the cable at (x, y) goes in the list. `way` is the cable's
 * waypoints: its first anchor, its bends, its last anchor. Each hop is drawn as an elbow, so
 * the pointer is matched against both legs of it.
 */
export function bendIndexAt(way: readonly (readonly number[])[], x: number, y: number): number {
  let best = 0;
  let bestD = Infinity;
  for (let i = 0; i + 1 < way.length; i++) {
    const a = way[i] as readonly number[];
    const b = way[i + 1] as readonly number[];
    const elbow = [b[0] ?? 0, a[1] ?? 0];
    const d = Math.min(segDist(x, y, a, elbow), segDist(x, y, elbow, b));
    if (d < bestD) {
      bestD = d;
      best = i;
    }
  }
  return best;
}

/** Whether an end names a box or frame, and (for a typed strip) a port that exists. */
export function endIsValid(doc: MapDocument, end: MapCableEnd): boolean {
  const owner = doc.elements.find((e) => e.id === end.element);
  if (owner?.kind !== "box" && owner?.kind !== "frame") return false;
  if (end.port === null) return true;
  if (owner.kind !== "box" || !owner.panel) return false;
  // A device strip names the device's own interfaces, which only the live data knows.
  return owner.panel.source === "device" || owner.panel.ports.some((p) => p.id === end.port);
}

// --- Ports ----------------------------------------------------------------------------

/**
 * Set (or clear) a box's port strip. A cable that named a typed port that no longer exists
 * keeps its box and loses the port, so the saved document stays valid.
 */
export function setBoxPorts(doc: MapDocument, boxId: string, ports: MapPanel | null): MapDocument {
  const next = patchElement(doc, boxId, { panel: ports });
  const known = new Set(ports?.ports.map((p) => p.id));
  const keep = (end: MapCableEnd): MapCableEnd =>
    end.element !== boxId || end.port === null || ports?.source === "device" || known.has(end.port)
      ? end
      : { ...end, port: null };
  return {
    ...next,
    elements: next.elements.map((e) =>
      e.kind === "cable" ? { ...e, from: keep(e.from), to: keep(e.to) } : e,
    ),
  };
}

/** Change a typed port's id, and the cable ends that name it. */
export function renamePort(doc: MapDocument, boxId: string, from: string, to: string): MapDocument {
  const rename = (end: MapCableEnd): MapCableEnd =>
    end.element === boxId && end.port === from ? { ...end, port: to } : end;
  return {
    ...doc,
    elements: doc.elements.map((e) => {
      if (e.kind === "cable") return { ...e, from: rename(e.from), to: rename(e.to) };
      if (e.id === boxId && e.kind === "box" && e.panel) {
        const ports = e.panel.ports.map((p) => (p.id === from ? { ...p, id: to } : p));
        return { ...e, panel: { ...e.panel, ports } };
      }
      return e;
    }),
  };
}

/** The lowest unused id like `p1` among a strip's typed ports. */
export function newPortId(ports: readonly { id: string }[]): string {
  const used = new Set(ports.map((p) => p.id));
  for (let n = 1; ; n++) if (!used.has(`p${n}`)) return `p${n}`;
}

// --- VLANs ----------------------------------------------------------------------------

/** How many ports and cables use a VLAN. */
export function vlanUsage(doc: MapDocument, id: string): number {
  let n = 0;
  for (const e of doc.elements) {
    if (e.kind === "cable" && e.vlan === id) n++;
    if (e.kind === "box") {
      for (const p of e.panel?.ports ?? []) {
        if (p.vlans.untagged === id || p.vlans.tagged.includes(id)) n++;
      }
    }
  }
  return n;
}

/** Apply `f` to every port's VLANs and every cable's VLAN. */
function mapVlanRefs(doc: MapDocument, f: (id: string) => string | null): MapDocument {
  const one = (v: string | null) => (v === null ? null : f(v));
  return {
    ...doc,
    elements: doc.elements.map((e) => {
      if (e.kind === "cable") return { ...e, vlan: one(e.vlan) };
      if (e.kind !== "box" || !e.panel) return e;
      const ports = e.panel.ports.map((p) => ({
        ...p,
        vlans: {
          untagged: one(p.vlans.untagged),
          tagged: p.vlans.tagged.flatMap((t) => f(t) ?? []),
        },
      }));
      return { ...e, panel: { ...e.panel, ports } };
    }),
  };
}

/** Remove a VLAN and clear it from every port and cable. */
export function removeVlan(doc: MapDocument, id: string): MapDocument {
  const cleared = mapVlanRefs(doc, (v) => (v === id ? null : v));
  return { ...cleared, vlans: doc.vlans.filter((v) => v.id !== id) };
}

/** Change a VLAN's id, and everything that names it. */
export function renameVlan(doc: MapDocument, from: string, to: string): MapDocument {
  const moved = mapVlanRefs(doc, (v) => (v === from ? to : v));
  return { ...moved, vlans: doc.vlans.map((v) => (v.id === from ? { ...v, id: to } : v)) };
}

/** A new VLAN: the lowest unused number as its id, the least used palette colour. */
export function newVlan(doc: MapDocument): MapVlan {
  const used = new Set(doc.vlans.map((v) => v.id));
  let n = 1;
  while (used.has(String(n))) n++;
  const count = (c: string) => doc.vlans.filter((v) => v.color === c).length;
  const least = [...DEVICE_COLORS].sort((a, b) => count(a.name) - count(b.name))[0];
  return { id: String(n), name: `VLAN ${n}`, color: least?.name ?? "blue", subnet: "", note: "" };
}
