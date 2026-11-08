// The automatic right-angle route of a cable, and where its label goes. Both keep clear of the
// other things on the map (boxes with their name tabs, frames, panels): the cable leaves its
// edge, turns in the middle of the free gap between the two ends, and never crosses another
// element's measured rectangle. Cables with bends set by the user don't come here.

import type { Point, Rect } from "./geometry";

/** Room kept clear around an element, and above a box or frame for its name tab. */
const MARGIN = 4;
const TAB = 22;
const STEP = 4;
/** Extra air a route keeps from other elements when the gaps allow. */
const AIR = 10;

/** An element's rectangle with the room around it a cable keeps clear of. */
export function inflate(r: Rect, tabbed: boolean): Rect {
  const top = tabbed ? TAB : MARGIN;
  return { x: r.x - MARGIN, y: r.y - top, w: r.w + 2 * MARGIN, h: r.h + top + MARGIN };
}

/** Whether two rectangles share area (touching edges don't count). */
export const overlap = (a: Rect, b: Rect): boolean =>
  a.x + a.w > b.x && a.x < b.x + b.w && a.y + a.h > b.y && a.y < b.y + b.h;

/** The area a route between two ends can occupy (their joint span), with room for the air. */
export function nearby(a: Rect, b: Rect): Rect {
  const x = Math.min(a.x, b.x) - AIR - MARGIN;
  const y = Math.min(a.y, b.y) - AIR - TAB;
  return {
    x,
    y,
    w: Math.max(a.x + a.w, b.x + b.w) + AIR + MARGIN - x,
    h: Math.max(a.y + a.h, b.y + b.h) + AIR + MARGIN - y,
  };
}

/** Whether an axis-aligned segment passes through the inside of a rectangle. */
function hits(r: Rect, [ax, ay]: Point, [bx, by]: Point): boolean {
  return (
    Math.max(ax, bx) > r.x &&
    Math.min(ax, bx) < r.x + r.w &&
    Math.max(ay, by) > r.y &&
    Math.min(ay, by) < r.y + r.h
  );
}

const contains = (r: Rect, x: number, y: number) =>
  x > r.x && x < r.x + r.w && y > r.y && y < r.y + r.h;

const flip = (r: Rect): Rect => ({ x: r.y, y: r.x, w: r.h, h: r.w });

/** Candidate positions across an end, the middle first. A jack is only met in its middle. */
function columns(r: Rect, whole: boolean): number[] {
  const mid = r.x + r.w / 2;
  if (!whole || r.w < 24) return [mid];
  const out: number[] = [mid];
  for (let d = 6; mid - d >= r.x + 8; d += 6) out.push(mid - d, mid + d);
  return out;
}

/** The route in "vertical" terms: the cable runs mostly along y. */
function vertical(
  a: Rect,
  b: Rect,
  aWhole: boolean,
  bWhole: boolean,
  down: boolean,
  avoid: readonly Rect[],
  turnMargin: number,
): Point[] | null {
  const sy = down ? a.y + a.h : a.y;
  const ey = down ? b.y : b.y + b.h;
  // Leaving or entering through a top edge also clears the name tab.
  const lo = down ? sy + MARGIN : ey + MARGIN;
  const hi = down ? ey - turnMargin : sy - turnMargin;
  if (hi < lo) return null;
  // Long cables look at fewer lanes: about 60 across the span, never finer than STEP.
  const step = Math.max(STEP, Math.ceil((hi - lo) / 60 / 2) * 2);
  const startsA = columns(a, aWhole);
  const endsB = columns(b, bWhole);
  // Prefer a route with some air around it; squeeze through a narrow gap only when it must.
  const found = (pad: number) => {
    const room = avoid.map((o) => ({
      x: o.x - pad,
      y: o.y - pad,
      w: o.w + 2 * pad,
      h: o.h + 2 * pad,
    }));
    const clearV = (x: number, y0: number, y1: number) =>
      room.every((o) => !hits(o, [x, y0], [x, y1]));
    const out: { y: number; sx: number; ex: number }[] = [];
    for (let y = Math.ceil(lo); y <= hi; y += step) {
      const sx = startsA.find((x) => clearV(x, sy, y));
      if (sx === undefined) continue;
      const ex = endsB.find(
        (x) => clearV(x, y, ey) && room.every((o) => !hits(o, [sx, y], [x, y])),
      );
      if (ex !== undefined) out.push({ y, sx, ex });
    }
    return out;
  };
  const lanes = found(AIR);
  if (lanes.length === 0) lanes.push(...found(0));
  if (lanes.length === 0) return null;
  // A run is neighbouring lanes with the same columns: one gap the cable can turn in. The
  // shortest route wins (fewest sideways steps), then the run nearest the middle of the span;
  // the cable turns in the middle of its run.
  const runs: (typeof lanes)[] = [];
  for (const l of lanes) {
    const run = runs[runs.length - 1];
    const last = run?.[run.length - 1];
    if (run && last && l.y - last.y <= step && l.sx === last.sx && l.ex === last.ex) run.push(l);
    else runs.push([l]);
  }
  const target = (lo + hi) / 2;
  const middle = (run: typeof lanes) => run[Math.floor(run.length / 2)] as (typeof lanes)[number];
  const cost = (run: typeof lanes) => Math.abs(middle(run).sx - middle(run).ex);
  const best = runs.reduce((p, r) =>
    cost(r) < cost(p) ||
    (cost(r) === cost(p) && Math.abs(middle(r).y - target) < Math.abs(middle(p).y - target))
      ? r
      : p,
  );
  const { y, sx, ex } = middle(best);
  return sx === ex
    ? [
        [sx, sy],
        [ex, ey],
      ]
    : [
        [sx, sy],
        [sx, y],
        [ex, y],
        [ex, ey],
      ];
}

/**
 * A right-angle route from `a` to `b` that crosses none of `avoid`, or null when there is no
 * free lane (the caller then falls back to the plain middle turn). `horizontal`: the cable
 * runs mostly along x. `aWhole`/`bWhole`: the end is a whole box, so the cable may leave
 * from beside its middle; a jack is met where it is.
 */
export function avoidingRoute(
  a: Rect,
  b: Rect,
  opts: {
    horizontal: boolean;
    forward: boolean;
    aWhole: boolean;
    bWhole: boolean;
    avoid: readonly Rect[];
  },
): Point[] | null {
  const { horizontal, forward, aWhole, bWhole } = opts;
  // The two ends' own surroundings don't count, nor does anything the ends sit inside.
  const ca: Point = [a.x + a.w / 2, a.y + a.h / 2];
  const cb: Point = [b.x + b.w / 2, b.y + b.h / 2];
  // Only what lies within the span of the two ends can be in the way.
  const zone = nearby(a, b);
  const avoid = opts.avoid.filter(
    (o) => overlap(o, zone) && !contains(o, ca[0], ca[1]) && !contains(o, cb[0], cb[1]),
  );
  if (!horizontal) return vertical(a, b, aWhole, bWhole, forward, avoid, TAB + MARGIN);
  const pts = vertical(flip(a), flip(b), aWhole, bWhole, forward, avoid.map(flip), MARGIN);
  return pts && pts.map(([x, y]): Point => [y, x]);
}

/**
 * Where a cable's label goes: on the free stretch of its route nearest the middle of its
 * longest segment, clear of every element and of the labels placed before it. Null when no
 * stretch is free (the caller then uses the middle of the longest segment).
 */
export function placeLabel(
  points: readonly Point[],
  width: number,
  avoid: readonly Rect[],
  taken: readonly Rect[],
): { at: Point; rect: Rect } | null {
  const segments = points
    .slice(1)
    .map((p, i) => ({ a: points[i] as Point, b: p }))
    .map((s) => ({ ...s, len: Math.abs(s.b[0] - s.a[0]) + Math.abs(s.b[1] - s.a[1]) }))
    .filter((s) => s.len >= 24)
    .sort((x, y) => y.len - x.len);
  // The text sits to the right of the point and above it (see CablesLayer).
  const rectAt = (p: Point): Rect => ({ x: p[0] + 6, y: p[1] - 19, w: width, h: 17 });
  for (const s of segments) {
    for (const f of [0.5, 0.35, 0.65, 0.2, 0.8]) {
      const at: Point = [s.a[0] + (s.b[0] - s.a[0]) * f, s.a[1] + (s.b[1] - s.a[1]) * f];
      const rect = rectAt(at);
      if (!avoid.some((o) => overlap(o, rect)) && !taken.some((t) => overlap(t, rect))) {
        return { at, rect };
      }
    }
  }
  return null;
}
