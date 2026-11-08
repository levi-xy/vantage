// The figures a map box can show under a linked device (up to 3). A small set per
// integration (the first three are the default; the inspector picks from it by `id`).
// Every query is a fixed string written here, one per figure for the whole fleet, never
// one per box. They are the queries the device cards already use, grouped per instance.

import { useQueries } from "@tanstack/react-query";
import type { MapBox, QueryResult } from "../../api";
import { useApiClient } from "../../app/session";
import { INSTANT_REFETCH_MS } from "../../cards/metrics/useMetricQuery";
import {
  formatCelsius,
  formatDuration,
  formatMs,
  formatPercent,
  formatRatio,
} from "../../cards/metrics/format";
import { parseValue } from "../../cards/metrics/promql";

export interface FigureDef {
  /** Stored in a box's `figures` list. */
  id: string;
  label: string;
  /** Instant query returning one series per instance, labelled `vantage_instance_id`. */
  expr: string;
  format: (value: number | undefined) => string;
}

const BY = "by (vantage_instance_id)";
const REAL_FS = 'fstype!~"tmpfs|devtmpfs|overlay|squashfs|ramfs|fuse.*"';
const SNMP = '__name__=~"vantage_(snmp_device|mikrotik_swos)_';

const formatCount = (v: number | undefined): string =>
  v === undefined ? "–" : String(Math.round(v));

// A switch or NAS reached over SNMP: how many links are up and how long it has been up.
const SNMP_FIGURES: readonly FigureDef[] = [
  {
    id: "ports_up",
    label: "Ports up",
    expr: `count ${BY} ({${SNMP}if_oper_status"} == 1)`,
    format: formatCount,
  },
  {
    id: "uptime",
    label: "Uptime",
    expr: `max ${BY} ({${SNMP}uptime_seconds"})`,
    format: formatDuration,
  },
];

export const FIGURES: Readonly<Record<string, readonly FigureDef[]>> = {
  linux_host: [
    {
      id: "cpu",
      label: "CPU",
      expr: `100 * (1 - avg ${BY} (rate(node_cpu_seconds_total{mode="idle"}[5m])))`,
      format: formatPercent,
    },
    {
      id: "memory",
      label: "Memory",
      expr: `max ${BY} (100 * (1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes))`,
      format: formatPercent,
    },
    {
      id: "disk",
      label: "Disk",
      expr: `max ${BY} (100 * (1 - node_filesystem_avail_bytes{${REAL_FS}} / node_filesystem_size_bytes{${REAL_FS}}))`,
      format: formatPercent,
    },
    {
      id: "uptime",
      label: "Uptime",
      expr: `max ${BY} (time() - node_boot_time_seconds)`,
      format: formatDuration,
    },
  ],
  pfsense: [
    {
      id: "cpu",
      label: "CPU",
      expr: `max ${BY} (100 - cpu_usage_idle{cpu="cpu-total"})`,
      format: formatPercent,
    },
    { id: "memory", label: "Memory", expr: `max ${BY} (mem_used_percent)`, format: formatPercent },
    { id: "temp", label: "Temp", expr: `max ${BY} (temp_temp)`, format: formatCelsius },
    { id: "states", label: "States", expr: `max ${BY} (pf_entries)`, format: formatCount },
    { id: "uptime", label: "Uptime", expr: `max ${BY} (system_uptime)`, format: formatDuration },
  ],
  snmp_device: SNMP_FIGURES,
  mikrotik_swos: SNMP_FIGURES,
  proxmox: [
    {
      id: "cpu",
      label: "CPU",
      expr: `max ${BY} (100 * vantage_proxmox_node_cpu_ratio)`,
      format: formatPercent,
    },
    {
      id: "memory",
      label: "Memory",
      expr: `max ${BY} (100 * vantage_proxmox_node_memory_used_bytes / vantage_proxmox_node_memory_total_bytes)`,
      format: formatPercent,
    },
    {
      id: "storage",
      label: "Storage",
      expr: `max ${BY} (100 * vantage_proxmox_storage_used_bytes / vantage_proxmox_storage_total_bytes)`,
      format: formatPercent,
    },
    {
      id: "uptime",
      label: "Uptime",
      expr: `max ${BY} (vantage_proxmox_node_uptime_seconds)`,
      format: formatDuration,
    },
  ],
  ping_check: [
    {
      id: "latency",
      label: "Latency",
      expr: `max ${BY} (vantage_ping_check_rtt_avg_seconds)`,
      format: formatMs,
    },
    {
      id: "loss",
      label: "Loss",
      expr: `max ${BY} (vantage_ping_check_packet_loss_ratio)`,
      format: formatRatio,
    },
  ],
  http_check: [
    {
      id: "duration",
      label: "Response",
      expr: `max ${BY} (vantage_http_check_duration_seconds)`,
      format: formatMs,
    },
  ],
};

const MAX_FIGURES = 3;

/** The figures a box shows: the ones it names, else the integration's first defaults. */
export function boxFigures(box: Pick<MapBox, "figures">, typeId: string): FigureDef[] {
  const defs = FIGURES[typeId] ?? [];
  const named = box.figures
    .map((id) => defs.find((d) => d.id === id))
    .filter((d): d is FigureDef => d !== undefined);
  return (box.figures.length > 0 ? named : [...defs]).slice(0, MAX_FIGURES);
}

export type FigureValues = ReadonlyMap<FigureDef, ReadonlyMap<string, number>>;

function valuesByInstance(result: QueryResult | undefined): Map<string, number> {
  const out = new Map<string, number>();
  for (const s of result?.series ?? []) {
    const id = s.labels.vantage_instance_id;
    const v = parseValue(s.values[0]?.[1]);
    if (id && v !== undefined) out.set(id, v);
  }
  return out;
}

/** One shared fleet query per figure in use, refreshed like the dashboard's cards. */
export function useFigureValues(defs: readonly FigureDef[]): FigureValues {
  const client = useApiClient();
  return useQueries({
    queries: defs.map((def) => ({
      queryKey: ["query", "fleet", def.expr],
      queryFn: () => client.query({ query: def.expr }),
      refetchInterval: INSTANT_REFETCH_MS,
    })),
    combine: (results) =>
      new Map(defs.map((def, i) => [def, valuesByInstance(results[i]?.data)] as const)),
  });
}
