// Path maths for the small inline-SVG charts (Sparkline, AreaChart). Pure, so it is easy to
// test. Points are `[unix_seconds, value]`; the SVG's viewBox is `width` x `height` and is
// stretched to its box (preserveAspectRatio="none").

export type Point = [number, number];

export interface Box {
  /** x domain, Unix seconds. */
  start: number;
  end: number;
  /** y domain. */
  min: number;
  max: number;
  width: number;
  height: number;
}

function xy([t, v]: Point, b: Box): string {
  const xSpan = b.end - b.start || 1;
  const ySpan = b.max - b.min || 1;
  const x = ((t - b.start) / xSpan) * b.width;
  const y = b.height - ((v - b.min) / ySpan) * b.height;
  return `${x.toFixed(1)} ${y.toFixed(1)}`;
}

/** "M x y L x y ...", or "" for fewer than two points. */
export function linePath(points: Point[], box: Box): string {
  if (points.length < 2) return "";
  return points.map((p, i) => `${i ? "L" : "M"}${xy(p, box)}`).join(" ");
}

/** The line closed down to the bottom edge, for a filled area. */
export function areaPath(points: Point[], box: Box): string {
  const line = linePath(points, box);
  if (!line) return "";
  const first = points[0] as Point;
  const last = points[points.length - 1] as Point;
  const bottom = (t: number) => xy([t, box.min], box);
  return `${line} L${bottom(last[0])} L${bottom(first[0])} Z`;
}

/**
 * Gridline values from 0 to a round top at or above `max`: at most 5 lines, steps of
 * 1, 2, 2.5 or 5 times a power of ten. `[0, 1]` when there is nothing above 0.
 */
export function niceTicks(max: number): number[] {
  if (!Number.isFinite(max) || max <= 0) return [0, 1];
  const raw = max / 4;
  const power = 10 ** Math.floor(Math.log10(raw));
  const step = ([1, 2, 2.5, 5, 10].find((m) => m * power >= raw) ?? 10) * power;
  const top = Math.ceil(max / step - 1e-9) * step;
  const ticks: number[] = [];
  for (let i = 0; i * step <= top + step / 1e6; i++) ticks.push(i * step);
  return ticks;
}

/**
 * Full hours strictly inside (start, end), every `everyHours`, leaving out ones too close to
 * either edge to have room for a label.
 */
export function hourTicks(start: number, end: number, everyHours = 1): number[] {
  const span = end - start;
  if (span <= 0) return [];
  const margin = span * 0.06;
  const ticks: number[] = [];
  const first = new Date(start * 1000);
  first.setMinutes(0, 0, 0);
  for (let t = first.getTime() / 1000; t < end; t += 3600) {
    const hour = new Date(t * 1000).getHours();
    if (t > start + margin && t < end - margin && hour % everyHours === 0) ticks.push(t);
  }
  return ticks;
}

/** min, average and max of the values, or undefined when there are none. */
export function stats(points: Point[]): { min: number; avg: number; max: number } | undefined {
  if (points.length === 0) return undefined;
  let min = Infinity;
  let max = -Infinity;
  let sum = 0;
  for (const [, v] of points) {
    min = Math.min(min, v);
    max = Math.max(max, v);
    sum += v;
  }
  return { min, avg: sum / points.length, max };
}
