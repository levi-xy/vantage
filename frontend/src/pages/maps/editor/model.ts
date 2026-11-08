// The editor's pure document operations: new elements, removal, moving, snapping and the
// "start with my devices" layout. Nothing here touches React or the network.

import type { Category, Instance, MapDocument, MapElement, MapIcon } from "../../../api";

/** The backend's caps (backend/app/api/map_document.py). */
export const MAX_ELEMENTS = 500;
export const MAX_COORD = 20_000;
export const MIN_SIZE = 8;
export const MAX_SIZE = 4_000;
export const GRID = 16;

export type Geometry = Extract<MapElement, { x: number }>;
export type Rect = { x: number; y: number; w: number; h: number };

export const isGeometry = (e: MapElement): e is Geometry => e.kind !== "cable";

const SIZES: Record<Geometry["kind"], [number, number]> = {
  box: [224, 88],
  frame: [480, 280],
  text: [208, 80],
  table: [272, 144],
  legend: [224, 144],
  networks: [224, 144],
};

export const ICONS: readonly MapIcon[] = [
  "internet",
  "cloud",
  "router",
  "firewall",
  "switch",
  "server",
  "nas",
  "ap",
  "pc",
  "laptop",
  "phone",
  "camera",
  "printer",
  "iot",
  "generic",
];

const ICON_BY_CATEGORY: Partial<Record<Category, MapIcon>> = {
  firewall: "firewall",
  hypervisor: "server",
  storage: "nas",
  switch: "switch",
  tunnel: "cloud",
  cloud: "cloud",
  host: "server",
};

export const clamp = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v));
export const snapTo = (v: number) => Math.round(v / GRID) * GRID;

/** The lowest unused id like `b1` (letters, digits, `_` and `-`; short). */
export function newId(doc: MapDocument, prefix: string): string {
  const used = new Set(doc.elements.map((e) => e.id));
  for (let n = 1; ; n++) {
    const id = `${prefix}${n}`;
    if (!used.has(id)) return id;
  }
}

export type NewKind = "box" | "frame" | "text" | "table" | "legend" | "networks";

const PREFIX: Record<NewKind, string> = {
  box: "b",
  frame: "f",
  text: "t",
  table: "tb",
  legend: "l",
  networks: "n",
};

/** A new element with its default size, its top-left corner at (x, y). */
export function makeElement(
  doc: MapDocument,
  kind: NewKind,
  x: number,
  y: number,
  extra: { instanceId?: string | null; icon?: MapIcon } = {},
): Geometry {
  const [w, h] = SIZES[kind];
  const base = {
    id: newId(doc, PREFIX[kind]),
    x: clamp(Math.round(x), 0, MAX_COORD),
    y: clamp(Math.round(y), 0, MAX_COORD),
    w,
    h,
  };
  switch (kind) {
    case "box":
      return {
        ...base,
        kind,
        label: "",
        subtitle: "",
        shape: "card",
        role: "",
        icon: extra.icon ?? "generic",
        model: "",
        address: "",
        version: "",
        instance_id: extra.instanceId ?? null,
        show: { details: true, status: true, figures: true },
        figures: [],
        sections: [],
        color: null,
        panel: null,
        nas: null,
      };
    case "frame":
      return {
        ...base,
        kind,
        label: "",
        instance_id: null,
        color: null,
        guests: { auto: true, hidden: [], order: [], manual: [] },
        groups: [],
        bars: true,
        side: null,
        model: "",
        address: "",
        version: "",
      };
    case "text":
      return { ...base, kind, text: "Note", size: "m", panel: true };
    case "table":
      return {
        ...base,
        kind,
        title: "Table",
        columns: ["Name", "Value"],
        rows: [{ cells: ["", ""], color: null }],
        badges: [],
      };
    case "legend":
    case "networks":
      return { ...base, kind };
  }
}

export const canAdd = (doc: MapDocument, n = 1) => doc.elements.length + n <= MAX_ELEMENTS;

export const withElement = (doc: MapDocument, el: MapElement): MapDocument => ({
  ...doc,
  elements: [...doc.elements, el],
});

/** Remove elements, and with them every cable attached to a removed box or frame. */
export function removeElements(doc: MapDocument, ids: readonly string[]): MapDocument {
  const gone = new Set(ids);
  return {
    ...doc,
    elements: doc.elements.filter(
      (e) =>
        !gone.has(e.id) &&
        !(e.kind === "cable" && (gone.has(e.from.element) || gone.has(e.to.element))),
    ),
  };
}

/** Replace fields of one element. */
export function patchElement(
  doc: MapDocument,
  id: string,
  patch: Record<string, unknown>,
): MapDocument {
  return {
    ...doc,
    elements: doc.elements.map((e) => (e.id === id ? ({ ...e, ...patch } as MapElement) : e)),
  };
}

/** Put the given rectangles on their elements. */
export function applyRects(doc: MapDocument, rects: ReadonlyMap<string, Rect>): MapDocument {
  return {
    ...doc,
    elements: doc.elements.map((e) => {
      const r = rects.get(e.id);
      return r && isGeometry(e) ? { ...e, ...r } : e;
    }),
  };
}

export function rectsOf(doc: MapDocument, ids: readonly string[]): Map<string, Rect> {
  const out = new Map<string, Rect>();
  for (const e of doc.elements) {
    if (ids.includes(e.id) && isGeometry(e)) out.set(e.id, { x: e.x, y: e.y, w: e.w, h: e.h });
  }
  return out;
}

export function union(rects: Iterable<Rect>): Rect {
  let x0 = Infinity;
  let y0 = Infinity;
  let x1 = -Infinity;
  let y1 = -Infinity;
  for (const r of rects) {
    x0 = Math.min(x0, r.x);
    y0 = Math.min(y0, r.y);
    x1 = Math.max(x1, r.x + r.w);
    y1 = Math.max(y1, r.y + r.h);
  }
  return { x: x0, y: y0, w: x1 - x0, h: y1 - y0 };
}

export interface Guides {
  /** x positions of vertical guide lines. */
  v: number[];
  /** y positions of horizontal guide lines. */
  h: number[];
}

/**
 * Pull `moving` onto the edges and centres of `others` when one is within `threshold`.
 * Returns the correction to add and the guide lines to draw.
 */
export function alignTo(
  moving: Rect,
  others: readonly Rect[],
  threshold: number,
): { dx: number; dy: number; guides: Guides } {
  const pull = (own: number[], theirs: number[]) => {
    let best: { d: number; at: number } | null = null;
    for (const o of own) {
      for (const t of theirs) {
        const d = t - o;
        if (Math.abs(d) <= threshold && (best === null || Math.abs(d) < Math.abs(best.d))) {
          best = { d, at: t };
        }
      }
    }
    return best;
  };
  const xs = (r: Rect) => [r.x, r.x + r.w / 2, r.x + r.w];
  const ys = (r: Rect) => [r.y, r.y + r.h / 2, r.y + r.h];
  const bx = pull(xs(moving), others.flatMap(xs));
  const by = pull(ys(moving), others.flatMap(ys));
  return {
    dx: bx?.d ?? 0,
    dy: by?.d ?? 0,
    guides: { v: bx ? [bx.at] : [], h: by ? [by.at] : [] },
  };
}

export type Handle = "n" | "s" | "e" | "w" | "ne" | "nw" | "se" | "sw";
export const HANDLES: readonly Handle[] = ["nw", "n", "ne", "e", "se", "s", "sw", "w"];
const MIN_DRAG_SIZE = 24;

/** The rectangle after dragging `handle` by (dx, dy), optionally on the grid. */
export function resizeRect(r: Rect, handle: Handle, dx: number, dy: number, snap: boolean): Rect {
  let left = r.x;
  let top = r.y;
  let right = r.x + r.w;
  let bottom = r.y + r.h;
  const fix = (v: number) => (snap ? snapTo(v) : Math.round(v));
  if (handle.includes("w")) left = fix(left + dx);
  if (handle.includes("e")) right = fix(right + dx);
  if (handle.includes("n")) top = fix(top + dy);
  if (handle.includes("s")) bottom = fix(bottom + dy);
  left = clamp(left, 0, right - MIN_DRAG_SIZE);
  top = clamp(top, 0, bottom - MIN_DRAG_SIZE);
  right = clamp(right, left + MIN_DRAG_SIZE, left + MAX_SIZE);
  bottom = clamp(bottom, top + MIN_DRAG_SIZE, top + MAX_SIZE);
  return { x: left, y: top, w: right - left, h: bottom - top };
}

/** Devices that no box or frame of this map is linked to. */
export function notOnMap(doc: MapDocument, instances: readonly Instance[]): Instance[] {
  const linked = new Set<string>();
  for (const e of doc.elements) {
    if ((e.kind === "box" || e.kind === "frame") && e.instance_id) linked.add(e.instance_id);
  }
  return instances.filter((i) => !linked.has(i.id));
}

export function iconFor(category: Category | undefined): MapIcon {
  return (category && ICON_BY_CATEGORY[category]) || "generic";
}

const COLUMNS = 4;
const GAP = 24;
const SITE_GAP = 48;

/**
 * "Start with my devices": one linked box per device in rows of four, grouped by site
 * (devices without a site last), no cables.
 */
export function devicesDocument(
  instances: readonly Instance[],
  categoryOf: (typeId: string) => Category | undefined,
): MapDocument {
  const bySite = new Map<string, Instance[]>();
  for (const i of instances) {
    const key = i.site ?? "";
    bySite.set(key, [...(bySite.get(key) ?? []), i]);
  }
  const sites = [...bySite.keys()].sort((a, b) =>
    a === "" ? 1 : b === "" ? -1 : a.localeCompare(b),
  );
  const [w, h] = SIZES.box;
  const elements: MapElement[] = [];
  let y = 0;
  for (const site of sites) {
    const devices = bySite.get(site) ?? [];
    devices.forEach((inst, n) => {
      if (elements.length >= MAX_ELEMENTS) return;
      const box = makeElement(
        { version: 1, vlans: [], elements },
        "box",
        (n % COLUMNS) * (w + GAP),
        y + Math.floor(n / COLUMNS) * (h + GAP),
        { instanceId: inst.id, icon: iconFor(categoryOf(inst.type_id)) },
      );
      elements.push(box);
    });
    y += Math.ceil(devices.length / COLUMNS) * (h + GAP) + SITE_GAP - GAP;
  }
  return { version: 1, vlans: [], elements };
}
