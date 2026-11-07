// Which history charts a device gets, by its type, and the PromQL behind each. Pure, so it is
// easy to test. Metric names are the ones the cards already read (see the card of the same
// device type); polled metrics are `vantage_<type_id>_*` (counters end in `_total`), scraped
// exporters keep their native names. Every query is scoped to the instance's id.
//
// One query per chart: charts with two lines (in/out) tag them with a label_replace'd label,
// so a page costs 2 to 5 queries, not one per line.

import type { AreaChartSeries, Tone } from "../../../cards/metrics/Charts";
import { parseValue, scopedQuery } from "../../../cards/metrics/promql";
import type { QueryResult } from "../../../api";
import type { Selector } from "../../../cards/metrics/promql";
import { snmpMetric } from "../../../cards/snmp-overview/snmpMetrics";
import { promqlString } from "../../../cards/throughput/ports";
import { rangeWindow } from "../../../cards/metrics/useMetricQuery";

/** Points per series (the query proxy allows 1 100). */
export const HISTORY_POINTS = 360;
/** rate() looks back at least 5 minutes (what the cards use) and never less than a step. */
const MIN_RATE_SECONDS = 300;

export type ValueFormat = "rate" | "percent" | "ms" | "count" | "celsius";

/** How a result's series become lines: by the value of `label`, shown as `names[value]`. */
export interface Split {
  label: string;
  names?: Record<string, string>;
}

export interface IfaceSource {
  /** The label naming the interface in the metrics. */
  label: string;
  /** Instant query ranking the interfaces by their traffic over the window. */
  list: (sel: Selector, windowSeconds: number) => string;
}

export interface HistoryChartDef {
  key: string;
  title: string;
  format: ValueFormat;
  /** `iface` is the chosen interface; undefined = all of them added up. */
  build: (sel: Selector, windowSeconds: number, iface?: string) => string;
  split?: Split;
  ifaces?: IfaceSource;
}

/** The rate() range (a PromQL duration) for a window. */
export function rateRange(windowSeconds: number): string {
  const { step } = rangeWindow(windowSeconds, 0, HISTORY_POINTS);
  return `${Math.max(MIN_RATE_SECONDS, step)}s`;
}

const IN_OUT: Split = { label: "direction", names: { in: "In", out: "Out" } };

/** Tags a vector with `label="value"`. */
const tag = (expr: string, label: string, value: string) =>
  `label_replace(${expr}, ${promqlString(label)}, ${promqlString(value)}, "", "")`;

/** In and out in one query: bits per second of two counters. */
function inOut(inSel: string, outSel: string, w: string): string {
  return `${tag(`8 * sum(rate(${inSel}[${w}]))`, "direction", "in")} or ${tag(`8 * sum(rate(${outSel}[${w}]))`, "direction", "out")}`;
}

const pct = (key: string, title: string, build: HistoryChartDef["build"], split?: Split) => ({
  key,
  title,
  format: "percent" as const,
  build,
  split,
});

// Real interfaces only, as the Throughput card's port lists leave out the virtual ones.
const PFSENSE_IF = 'interface!~"lo.*|pflog.*|pfsync.*|enc.*"';
const NODE_NIC = 'device!~"lo|veth.*|docker.*|br-.*|virbr.*"';
const NODE_FS = 'fstype!~"tmpfs|devtmpfs|overlay|squashfs|ramfs|fuse.*"';

const matchIface = (label: string, base: string, iface?: string) =>
  iface === undefined ? base : `${base ? `${base},` : ""}${label}=${promqlString(iface)}`;

const pfsenseTraffic: HistoryChartDef = {
  key: "traffic",
  title: "Traffic",
  format: "rate",
  split: IN_OUT,
  build: (sel, w, iface) => {
    const m = sel(matchIface("interface", PFSENSE_IF, iface));
    return inOut(`net_bytes_recv${m}`, `net_bytes_sent${m}`, rateRange(w));
  },
  ifaces: {
    label: "interface",
    list: (sel, w) => {
      const r = `${w}s`;
      const m = sel(PFSENSE_IF);
      return `sum by (interface) (rate(net_bytes_recv${m}[${r}])) + sum by (interface) (rate(net_bytes_sent${m}[${r}]))`;
    },
  },
};

function snmpTraffic(): HistoryChartDef {
  return {
    key: "traffic",
    title: "Traffic",
    format: "rate",
    split: IN_OUT,
    build: (sel, w, iface) => {
      const extra = iface === undefined ? undefined : `if_name=${promqlString(iface)}`;
      return inOut(
        snmpMetric(sel, "if_in_bytes_total", extra),
        snmpMetric(sel, "if_out_bytes_total", extra),
        rateRange(w),
      );
    },
    ifaces: {
      label: "if_name",
      list: (sel, w) =>
        `sum by (if_name) (rate(${snmpMetric(sel, "if_in_bytes_total")}[${w}s])) + sum by (if_name) (rate(${snmpMetric(sel, "if_out_bytes_total")}[${w}s]))`,
    },
  };
}

const P = "vantage_proxmox_";

const BY_TYPE: Record<string, () => HistoryChartDef[]> = {
  pfsense: () => [
    pfsenseTraffic,
    pct("cpu", "CPU", (sel) => `100 - cpu_usage_idle${sel('cpu="cpu-total"')}`),
    pct("mem", "Memory", (sel) => `mem_used_percent${sel()}`),
  ],
  snmp_device: () => [snmpTraffic()],
  mikrotik_swos: () => [
    snmpTraffic(),
    {
      key: "temp",
      title: "Temperature",
      format: "celsius",
      build: (sel) => `vantage_mikrotik_swos_temperature_celsius${sel()}`,
    },
  ],
  linux_host: () => [
    pct(
      "cpu",
      "CPU",
      (sel, w) =>
        `100 * (1 - avg(rate(node_cpu_seconds_total${sel('mode="idle"')}[${rateRange(w)}])))`,
    ),
    pct(
      "mem",
      "Memory",
      (sel) =>
        `100 * (1 - node_memory_MemAvailable_bytes${sel()} / node_memory_MemTotal_bytes${sel()})`,
    ),
    pct(
      "disk",
      "Disk used (fullest filesystem)",
      (sel) =>
        `max(100 * (1 - node_filesystem_avail_bytes${sel(NODE_FS)} / node_filesystem_size_bytes${sel(NODE_FS)}))`,
    ),
    {
      key: "net",
      title: "Network",
      format: "rate",
      split: IN_OUT,
      build: (sel, w) =>
        inOut(
          `node_network_receive_bytes_total${sel(NODE_NIC)}`,
          `node_network_transmit_bytes_total${sel(NODE_NIC)}`,
          rateRange(w),
        ),
    },
  ],
  proxmox: () => {
    const node: Split = { label: "node" };
    return [
      pct("cpu", "CPU", (sel) => `100 * ${P}node_cpu_ratio${sel()}`, node),
      pct(
        "mem",
        "Memory",
        (sel) => `100 * ${P}node_memory_used_bytes${sel()} / ${P}node_memory_total_bytes${sel()}`,
        node,
      ),
      pct(
        "disk",
        "Root disk used",
        (sel) =>
          `100 * ${P}node_root_disk_used_bytes${sel()} / ${P}node_root_disk_total_bytes${sel()}`,
        node,
      ),
      {
        key: "net",
        title: "Guest network traffic",
        format: "rate",
        split: IN_OUT,
        build: (sel, w) =>
          inOut(
            `${P}guest_network_receive_bytes_total${sel()}`,
            `${P}guest_network_transmit_bytes_total${sel()}`,
            rateRange(w),
          ),
      },
    ];
  },
  ping_check: () => [
    {
      key: "rtt",
      title: "Round-trip time",
      format: "ms",
      build: (sel) => `vantage_ping_check_rtt_avg_seconds${sel()}`,
    },
    pct("loss", "Packet loss", (sel) => `100 * vantage_ping_check_packet_loss_ratio${sel()}`),
  ],
  http_check: () => [
    {
      key: "duration",
      title: "Response time",
      format: "ms",
      build: (sel) => `vantage_http_check_duration_seconds${sel()}`,
    },
    // success is 0 or 1; its average over a step is the share of the time the check passed.
    pct(
      "up",
      "Availability",
      (sel, w) => `100 * avg_over_time(vantage_http_check_success${sel()}[${rateRange(w)}])`,
    ),
  ],
  emby: () => [
    {
      key: "streams",
      title: "Streams",
      format: "count",
      build: (sel) => `sum by (play_method) (vantage_emby_streams${sel()})`,
      split: {
        label: "play_method",
        names: {
          direct_play: "Direct play",
          direct_stream: "Direct stream",
          transcode: "Transcode",
        },
      },
    },
  ],
  smart_exporter: () => [
    {
      key: "temp",
      title: "Disk temperature",
      format: "celsius",
      build: (sel) => `smartctl_device_temperature${sel('temperature_type="current"')}`,
      split: { label: "device" },
    },
  ],
  cloudflared: () => [
    {
      key: "requests",
      title: "Requests and errors",
      format: "count",
      split: { label: "series", names: { requests: "Requests/s", errors: "Errors/s" } },
      build: (sel, w) => {
        const r = rateRange(w);
        return `${tag(`sum(rate(cloudflared_tunnel_total_requests${sel()}[${r}]))`, "series", "requests")} or ${tag(`sum(rate(cloudflared_tunnel_request_errors${sel()}[${r}]))`, "series", "errors")}`;
      },
    },
  ],
  cloudflare: (): HistoryChartDef[] => [
    {
      key: "requests",
      title: "Requests per hour",
      format: "count",
      split: { label: "series", names: { requests: "Requests", cached: "Cached" } },
      // Hourly totals of the last complete UTC hour, read as they are (never with rate()).
      build: (sel) =>
        `${tag(`sum(vantage_cloudflare_zone_requests${sel()})`, "series", "requests")} or ${tag(`sum(vantage_cloudflare_zone_cached_requests${sel()})`, "series", "cached")}`,
    },
    {
      key: "errors",
      title: "Error responses per hour",
      format: "count",
      split: { label: "status_class", names: { "4xx": "4xx", "5xx": "5xx" } },
      build: (sel) =>
        `sum by (status_class) (vantage_cloudflare_zone_requests_by_status_class${sel('status_class=~"4xx|5xx"')})`,
    },
    {
      key: "tunnels",
      title: "Tunnel connections",
      format: "count",
      build: (sel) => `sum(vantage_cloudflare_tunnel_connections${sel()})`,
    },
  ],
};

/** The charts for a device type; empty for a type with no history. */
export function historyChartsFor(typeId: string): HistoryChartDef[] {
  return BY_TYPE[typeId]?.() ?? [];
}

/** The chart's PromQL for one instance. */
export function chartQuery(
  instanceId: string,
  chart: HistoryChartDef,
  windowSeconds: number,
  iface?: string,
): string {
  return scopedQuery(instanceId, (sel) => chart.build(sel, windowSeconds, iface));
}

/** The interface-ranking query for one instance. */
export function ifaceListQuery(instanceId: string, source: IfaceSource, windowSeconds: number) {
  return scopedQuery(instanceId, (sel) => source.list(sel, windowSeconds));
}

/** Lines' tones, in order: blue, orange, then the neutral one. */
const TONES: Tone[] = ["in", "out", "line"];

/** The result's series as chart lines, named and toned by the chart's split. */
export function chartSeries(
  chart: HistoryChartDef,
  result: QueryResult | undefined,
): AreaChartSeries[] {
  const split = chart.split;
  const order = split?.names ? Object.keys(split.names) : [];
  const rows = (result?.series ?? []).map((s) => {
    const key = split ? (s.labels[split.label] ?? "") : "";
    return {
      key,
      name: split ? (split.names?.[key] ?? (key || chart.title)) : chart.title,
      points: s.values.flatMap(([t, raw]) => {
        const v = parseValue(raw);
        return v === undefined ? [] : [[t, v] as [number, number]];
      }),
    };
  });
  rows.sort((a, b) => {
    const ia = order.indexOf(a.key);
    const ib = order.indexOf(b.key);
    return ia >= 0 && ib >= 0 ? ia - ib : a.name.localeCompare(b.name);
  });
  return rows.map((r, i): AreaChartSeries => ({
    name: r.name,
    points: r.points,
    tone: TONES[i % TONES.length] ?? "line",
    area: i === 0,
  }));
}
