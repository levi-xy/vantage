// Pure drawing geometry for the map: where a cable runs, how thick it is, how big the
// drawing is. Cables are anchored at a box edge (ports come with M9.2b) and made of
// horizontal and vertical segments only.

import type { MapCable, MapDocument, MapSpeed } from "../../api";

export interface Rect {
  x: number;
  y: number;
  w: number;
  h: number;
}
export type Point = readonly [number, number];

/** Stroke width by speed, 100M thin to 10G+ thick. */
const WIDTH: Record<MapSpeed, number> = {
  "100M": 1,
  "1G": 1.5,
  "2.5G": 2.5,
  "5G": 3,
  "10G": 4,
  "25G": 5,
  "40G": 6,
  "100G": 7,
  wifi: 1.5,
  virtual: 1.5,
};

export const cableWidth = (speed: MapSpeed | null): number => (speed ? WIDTH[speed] : 1.5);

export const SPEED_ORDER: readonly MapSpeed[] = [
  "100M",
  "1G",
  "2.5G",
  "5G",
  "10G",
  "25G",
  "40G",
  "100G",
  "wifi",
  "virtual",
];

export function speedLabel(speed: MapSpeed): string {
  if (speed === "wifi") return "Wi-Fi";
  if (speed === "virtual") return "Virtual";
  return speed;
}

const center = (r: Rect): Point => [r.x + r.w / 2, r.y + r.h / 2];

/** The midpoint of the edge of `r` that faces `toward`. */
export function anchor(r: Rect, toward: Point): Point {
  const [cx, cy] = center(r);
  const dx = toward[0] - cx;
  const dy = toward[1] - cy;
  if (Math.abs(dx) * r.h >= Math.abs(dy) * r.w) {
    return [dx >= 0 ? r.x + r.w : r.x, cy];
  }
  return [cx, dy >= 0 ? r.y + r.h : r.y];
}

/** Insert an elbow wherever two neighbours are not on one horizontal or vertical line. */
function orthogonal(points: Point[]): Point[] {
  const out: Point[] = [points[0] as Point];
  for (let i = 1; i < points.length; i++) {
    const a = out[out.length - 1] as Point;
    const b = points[i] as Point;
    if (a[0] !== b[0] && a[1] !== b[1]) out.push([b[0], a[1]]);
    out.push(b);
  }
  return out;
}

/** The points of a cable from one box to another, through its bends. */
export function cablePath(a: Rect, b: Rect, bends: readonly Point[]): Point[] {
  if (bends.length > 0) {
    const first = bends[0] as Point;
    const last = bends[bends.length - 1] as Point;
    return orthogonal([anchor(a, first), ...bends, anchor(b, last)]);
  }
  const [acx, acy] = center(a);
  const [bcx, bcy] = center(b);
  const horizontal =
    Math.abs(bcx - acx) * Math.max(a.h, b.h) >= Math.abs(bcy - acy) * Math.max(a.w, b.w);
  if (horizontal) {
    const start: Point = [bcx >= acx ? a.x + a.w : a.x, acy];
    const end: Point = [bcx >= acx ? b.x : b.x + b.w, bcy];
    if (start[1] === end[1]) return [start, end];
    const mid = Math.round((start[0] + end[0]) / 2);
    return [start, [mid, start[1]], [mid, end[1]], end];
  }
  const start: Point = [acx, bcy >= acy ? a.y + a.h : a.y];
  const end: Point = [bcx, bcy >= acy ? b.y : b.y + b.h];
  if (start[0] === end[0]) return [start, end];
  const mid = Math.round((start[1] + end[1]) / 2);
  return [start, [start[0], mid], [end[0], mid], end];
}

export const pathData = (points: readonly Point[]): string =>
  points.map(([x, y], i) => `${i === 0 ? "M" : "L"} ${x} ${y}`).join(" ");

/** Where a cable's label goes: the middle of its longest segment. */
export function labelPoint(points: readonly Point[]): Point {
  let best: Point = points[0] as Point;
  let longest = -1;
  for (let i = 1; i < points.length; i++) {
    const a = points[i - 1] as Point;
    const b = points[i] as Point;
    const len = Math.abs(b[0] - a[0]) + Math.abs(b[1] - a[1]);
    if (len > longest) {
      longest = len;
      best = [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2];
    }
  }
  return best;
}

/** Where the small speed tag goes: a little way in from the target end. */
export function tagPoint(points: readonly Point[]): Point {
  const end = points[points.length - 1] as Point;
  const prev = points[points.length - 2] as Point;
  const dx = Math.sign(end[0] - prev[0]);
  const dy = Math.sign(end[1] - prev[1]);
  return [end[0] - dx * 22 + (dy !== 0 ? 8 : 0), end[1] - dy * 22 - (dx !== 0 ? 6 : 0)];
}

/** Boxes and frames by id: the things a cable can join. */
export function endRects(doc: MapDocument): Map<string, Rect> {
  const out = new Map<string, Rect>();
  for (const e of doc.elements) {
    if (e.kind === "box" || e.kind === "frame") out.set(e.id, { x: e.x, y: e.y, w: e.w, h: e.h });
  }
  return out;
}

export function cableEnds(cable: MapCable, rects: Map<string, Rect>): [Rect, Rect] | null {
  const a = rects.get(cable.from.element);
  const b = rects.get(cable.to.element);
  return a && b ? [a, b] : null;
}

/** The size the drawing needs, with a margin. */
export function bounds(doc: MapDocument): Rect {
  let minX = Infinity;
  let minY = Infinity;
  let maxX = 0;
  let maxY = 0;
  const grow = (x: number, y: number, w = 0, h = 0) => {
    minX = Math.min(minX, x);
    minY = Math.min(minY, y);
    maxX = Math.max(maxX, x + w);
    maxY = Math.max(maxY, y + h);
  };
  for (const e of doc.elements) {
    if (e.kind === "cable") for (const [x, y] of e.bends) grow(x, y);
    else grow(e.x, e.y, e.w, e.h);
  }
  if (minX === Infinity) return { x: 0, y: 0, w: 0, h: 0 };
  return { x: minX, y: minY, w: maxX - minX, h: maxY - minY };
}

/** Elements in drawing order: top to bottom, then left to right. */
export function drawingOrder<T extends { x: number; y: number }>(items: T[]): T[] {
  return [...items].sort((a, b) => a.y - b.y || a.x - b.x);
}
