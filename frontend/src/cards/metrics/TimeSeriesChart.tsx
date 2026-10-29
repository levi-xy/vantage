import { useEffect, useRef } from "react";
import { init, use as register } from "echarts/core";
import type { EChartsType } from "echarts/core";
import { LineChart } from "echarts/charts";
import { GridComponent, TooltipComponent } from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";
import { useTheme } from "../../app/ThemeProvider";
import { buildOption } from "./chartOption";
import type { TimeSeriesChartProps } from "./chartOption";

// Only what a line/area chart needs, to keep the bundle small.
register([LineChart, GridComponent, TooltipComponent, CanvasRenderer]);

/** A line/area chart that follows light/dark mode and resizes with its container. */
export function TimeSeriesChart({ label, ...props }: TimeSeriesChartProps) {
  const { theme } = useTheme();
  const element = useRef<HTMLDivElement>(null);
  const chart = useRef<EChartsType | null>(null);

  // One chart per theme: ECharts bakes colours in at init, so a theme change rebuilds it.
  useEffect(() => {
    const el = element.current;
    if (!el) return;
    const instance = init(el, undefined, { renderer: "canvas" });
    chart.current = instance;
    const observer =
      typeof ResizeObserver === "function" ? new ResizeObserver(() => instance.resize()) : null;
    observer?.observe(el);
    return () => {
      observer?.disconnect();
      instance.dispose();
      chart.current = null;
    };
  }, [theme]);

  useEffect(() => {
    chart.current?.setOption(buildOption(props, theme), true);
  }, [props.series, props.format, props.area, props.sparkline, theme]); // eslint-disable-line react-hooks/exhaustive-deps

  return <div ref={element} role="img" aria-label={label} className="h-full w-full min-h-0" />;
}
