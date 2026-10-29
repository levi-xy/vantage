import type { EChartsCoreOption } from "echarts/core";

export interface ChartSeries {
  name: string;
  /** `[unix_seconds, value]` pairs. */
  points: [number, number][];
}

export interface TimeSeriesChartProps {
  series: ChartSeries[];
  /** Formats axis labels and tooltip values. */
  format?: (value: number) => string;
  /** Filled area under the line. */
  area?: boolean;
  /** No axes, legend or tooltip: for a stat tile's sparkline. */
  sparkline?: boolean;
  /** Accessible description of the chart. */
  label: string;
}

const COLORS = {
  light: { line: ["#2563eb", "#d97706", "#059669"], text: "#475569", grid: "#e2e8f0" },
  dark: { line: ["#60a5fa", "#fbbf24", "#34d399"], text: "#94a3b8", grid: "#334155" },
};

export function buildOption(
  { series, format, area, sparkline }: Omit<TimeSeriesChartProps, "label">,
  theme: "light" | "dark",
): EChartsCoreOption {
  const c = COLORS[theme];
  const fmt = format ?? ((v: number) => String(v));
  return {
    animation: false,
    color: c.line,
    grid: sparkline
      ? { left: 0, right: 0, top: 2, bottom: 2 }
      : { left: 8, right: 8, top: 12, bottom: 4, containLabel: true },
    tooltip: sparkline
      ? { show: false }
      : {
          trigger: "axis",
          valueFormatter: (v: unknown) => (typeof v === "number" ? fmt(v) : String(v)),
        },
    xAxis: {
      type: "time",
      show: !sparkline,
      axisLabel: { color: c.text },
      axisLine: { lineStyle: { color: c.grid } },
    },
    yAxis: {
      type: "value",
      show: !sparkline,
      scale: sparkline,
      axisLabel: { color: c.text, formatter: (v: number) => fmt(v) },
      splitLine: { lineStyle: { color: c.grid } },
    },
    series: series.map((s) => ({
      name: s.name,
      type: "line",
      showSymbol: false,
      smooth: false,
      lineStyle: { width: sparkline ? 1.5 : 2 },
      areaStyle: area || sparkline ? { opacity: 0.15 } : undefined,
      data: s.points.map(([t, v]) => [t * 1000, v]),
    })),
  };
}
