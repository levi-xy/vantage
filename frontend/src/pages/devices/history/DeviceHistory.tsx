// The device page's History section: one range picker and a few charts chosen by the device's
// type. Every chart is one range query through the typed client's /query.

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import type { Instance, QueryResult } from "../../../api";
import { useApiClient } from "../../../app/session";
import { useUnits } from "../../../app/units";
import { AreaChart } from "../../../cards/metrics/Charts";
import { formatCelsius, formatMs, formatPercent } from "../../../cards/metrics/format";
import { parseValue } from "../../../cards/metrics/promql";
import { RangePicker } from "../../../cards/metrics/RangePicker";
import { RANGES } from "../../../cards/metrics/ranges";
import { useFleetRangeQuery } from "../../../cards/metrics/useMetricQuery";
import { panel } from "../../alerts/tone";
import {
  chartQuery,
  chartSeries,
  HISTORY_POINTS,
  historyChartsFor,
  ifaceListQuery,
} from "./historyCharts";
import type { HistoryChartDef, ValueFormat } from "./historyCharts";

const DEFAULT_RANGE = 2; // 24 h
const MAX_IFACES = 200;

function formatCount(v: number): string {
  return Number.isInteger(v) ? String(v) : v >= 10 ? v.toFixed(0) : v.toFixed(2);
}

function useFormat(kind: ValueFormat) {
  const { formatRate, unit } = useUnits();
  switch (kind) {
    case "rate":
      return { format: formatRate, tickScale: unit === "bytes" ? 8 : 1 };
    case "percent":
      return { format: formatPercent, tickScale: 1 };
    case "ms":
      return { format: formatMs, tickScale: 1 };
    case "celsius":
      return { format: formatCelsius, tickScale: 1 };
    default:
      return { format: formatCount, tickScale: 1 };
  }
}

/** Interfaces busiest first, from one instant query over the chosen window. */
function useInterfaces(instanceId: string, chart: HistoryChartDef, windowSeconds: number) {
  const client = useApiClient();
  const source = chart.ifaces;
  const expr = source ? ifaceListQuery(instanceId, source, windowSeconds) : "";
  const list = useQuery<QueryResult>({
    queryKey: ["query", "ifaces", expr],
    queryFn: () => client.query({ query: expr }),
    enabled: source !== undefined,
    staleTime: 60_000,
  });
  if (!source) return [];
  return (list.data?.series ?? [])
    .flatMap((s) => {
      const name = s.labels[source.label];
      const value = parseValue(s.values[0]?.[1]);
      return name ? [{ name, value: value ?? 0 }] : [];
    })
    .sort((a, b) => b.value - a.value || a.name.localeCompare(b.name, undefined, { numeric: true }))
    .slice(0, MAX_IFACES)
    .map((o) => o.name);
}

function HistoryChart({
  instanceId,
  chart,
  rangeIndex,
}: {
  instanceId: string;
  chart: HistoryChartDef;
  rangeIndex: number;
}) {
  const range = RANGES[rangeIndex] ?? RANGES[DEFAULT_RANGE];
  const [iface, setIface] = useState("");
  const ifaces = useInterfaces(instanceId, chart, range.seconds);
  // A port that isn't in the list any more (or none chosen) means all ports added up.
  const chosen = chart.ifaces && ifaces.includes(iface) ? iface : undefined;
  const { format, tickScale } = useFormat(chart.format);
  // The expression is built in code from the quoted instance id, never from user text.
  const query = useFleetRangeQuery(
    chartQuery(instanceId, chart, range.seconds, chosen),
    range.seconds,
    true,
    HISTORY_POINTS,
  );
  const end = query.dataUpdatedAt / 1000;
  let body;
  if (query.isError) {
    body = (
      <p role="alert" className="line-clamp-2 text-xs text-crit-text" title={query.error.message}>
        Could not load the history: {query.error.message}
      </p>
    );
  } else if (query.isPending) {
    body = <p className="text-xs text-muted">Loading…</p>;
  } else {
    body = (
      <AreaChart
        window={end > 0 ? { start: end - range.seconds, end } : undefined}
        label={`${chart.title} over the last ${range.text}`}
        format={format}
        tickScale={tickScale}
        series={chartSeries(chart, query.data)}
      />
    );
  }
  return (
    <div className={`${panel} flex min-w-0 flex-col gap-2 p-3`}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="text-sm font-semibold">{chart.title}</h3>
        {chart.ifaces && ifaces.length > 0 && (
          <select
            aria-label={`${chart.title}: port`}
            value={chosen ?? ""}
            onChange={(e) => setIface(e.target.value)}
            className="max-w-full min-w-0 rounded border border-line bg-panel px-1.5 py-0.5 text-xs"
          >
            <option value="">All ports</option>
            {ifaces.map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </select>
        )}
      </div>
      <div className="flex h-48 min-h-0 flex-col">{body}</div>
    </div>
  );
}

/** Nothing for a device type without history charts. */
export function DeviceHistory({ instance }: { instance: Instance }) {
  const [rangeIndex, setRangeIndex] = useState(DEFAULT_RANGE);
  const charts = historyChartsFor(instance.type_id);
  if (charts.length === 0) return null;
  return (
    <section aria-label="History" className="mb-4">
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-base font-semibold">History</h2>
        <RangePicker value={rangeIndex} onChange={setRangeIndex} />
      </div>
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        {charts.map((chart) => (
          <HistoryChart
            key={`${instance.id}:${chart.key}`}
            instanceId={instance.id}
            chart={chart}
            rangeIndex={rangeIndex}
          />
        ))}
      </div>
    </section>
  );
}
