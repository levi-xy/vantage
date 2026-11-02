// Small charts drawn as plain inline SVG paths: no chart library. The SVG stretches to its
// box; strokes keep their width (non-scaling-stroke). Colours are the palette's (`in`, `out`,
// `line`), so light and dark mode need nothing extra. Inside a stale card body the card's
// wrapper greys them.
import { areaPath, hourTicks, linePath, niceTicks } from "./chart";
import type { Box, Point } from "./chart";

const VIEW_W = 1000;
const VIEW_H = 100;

const STROKE = { in: "stroke-in", out: "stroke-out" } as const;
const FILL = { in: "fill-in/15", out: "fill-out/15" } as const;
type Tone = keyof typeof STROKE;

function domain(series: Point[][]): { start: number; end: number; max: number } {
  let start = Infinity;
  let end = -Infinity;
  let max = 0;
  for (const s of series) {
    for (const [t, v] of s) {
      start = Math.min(start, t);
      end = Math.max(end, t);
      max = Math.max(max, v);
    }
  }
  return { start, end, max };
}

export interface SparklineProps {
  points: Point[];
  /** Accessible description. */
  label: string;
  className?: string;
}

/** A filled line, no axes: under a stat's value. Scaled between the values' min and max. */
export function Sparkline({ points, label, className = "h-12" }: SparklineProps) {
  if (points.length < 2) return null;
  const { start, end } = domain([points]);
  let min = Infinity;
  let max = -Infinity;
  for (const [, v] of points) {
    min = Math.min(min, v);
    max = Math.max(max, v);
  }
  // A little headroom so a flat or peaking line isn't drawn on the edge.
  const pad = (max - min) * 0.1 || Math.abs(max) * 0.1 || 1;
  const box: Box = { start, end, min: min - pad, max: max + pad, width: VIEW_W, height: VIEW_H };
  return (
    <svg
      role="img"
      aria-label={label}
      viewBox={`0 0 ${VIEW_W} ${VIEW_H}`}
      preserveAspectRatio="none"
      className={`block w-full ${className}`}
    >
      <path d={areaPath(points, box)} className={FILL.in} />
      <path
        d={linePath(points, box)}
        fill="none"
        strokeWidth={1.5}
        vectorEffect="non-scaling-stroke"
        className={STROKE.in}
      />
    </svg>
  );
}

export interface AreaChartSeries {
  name: string;
  points: Point[];
  tone: Tone;
  /** Filled under the line. */
  area?: boolean;
}

export interface AreaChartProps {
  series: AreaChartSeries[];
  /** Formats the y-axis labels. */
  format: (value: number) => string;
  /**
   * Gridlines are round numbers in the shown unit: values are divided by this before
   * rounding (8 for a bit rate shown in bytes).
   */
  tickScale?: number;
  /** The x range (Unix seconds); by default the points' own. A gap at either end stays one. */
  window?: { start: number; end: number };
  label: string;
}

/** Lines over time from 0 up, with gridlines, y-axis labels and hour labels. Fills its box. */
export function AreaChart({ series, format, tickScale = 1, window, label }: AreaChartProps) {
  const drawn = series.filter((s) => s.points.length > 1);
  if (drawn.length === 0) {
    return <p className="text-xs text-muted">No history yet.</p>;
  }
  const own = domain(drawn.map((s) => s.points));
  const { start, end } = window ?? own;
  const { max } = own;
  const ticks = niceTicks(max / tickScale).map((t) => t * tickScale);
  const top = ticks[ticks.length - 1] ?? 1;
  const box: Box = { start, end, min: 0, max: top, width: VIEW_W, height: VIEW_H };
  const hours = hourTicks(start, end, end - start > 8 * 3600 ? 3 : 1);
  const hour = (t: number) =>
    new Date(t * 1000).toLocaleTimeString(undefined, {
      hour: "2-digit",
      minute: "2-digit",
      hourCycle: "h23",
    });
  return (
    <div className="grid min-h-0 flex-1 grid-cols-[auto_minmax(0,1fr)] grid-rows-[minmax(0,1fr)_auto] gap-x-2">
      <div className="relative min-w-10 text-right text-[11px] whitespace-nowrap text-muted">
        {/* Sizes the column; the labels sit at their gridlines. */}
        <span className="invisible">{format(top)}</span>
        {ticks.map((t) => (
          <span
            key={t}
            className="absolute right-0 -translate-y-1/2"
            style={{ top: `${100 - (t / top) * 100}%` }}
          >
            {format(t)}
          </span>
        ))}
      </div>
      <svg
        role="img"
        aria-label={label}
        viewBox={`0 0 ${VIEW_W} ${VIEW_H}`}
        preserveAspectRatio="none"
        className="block h-full min-h-16 w-full overflow-visible"
      >
        {ticks.map((t) => {
          const y = VIEW_H - (t / top) * VIEW_H;
          return (
            <line
              key={t}
              x1={0}
              x2={VIEW_W}
              y1={y}
              y2={y}
              strokeWidth={1}
              vectorEffect="non-scaling-stroke"
              className="stroke-line"
            />
          );
        })}
        {drawn.map((s) => (
          <g key={s.name}>
            {s.area && <path d={areaPath(s.points, box)} className={FILL[s.tone]} />}
            <path
              d={linePath(s.points, box)}
              fill="none"
              strokeWidth={2}
              vectorEffect="non-scaling-stroke"
              className={STROKE[s.tone]}
            />
          </g>
        ))}
      </svg>
      <span />
      <div className="relative h-5 pt-1.5 text-[11px] text-muted" aria-hidden="true">
        {hours.map((t) => (
          <span
            key={t}
            className="absolute -translate-x-1/2"
            style={{ left: `${((t - start) / (end - start)) * 100}%` }}
          >
            {hour(t)}
          </span>
        ))}
        <span className="absolute right-0">now</span>
      </div>
    </div>
  );
}
