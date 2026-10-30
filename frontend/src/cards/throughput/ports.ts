// Ports the throughput card can watch, how a choice is stored in the card's settings, and
// the PromQL for their traffic. Pure functions, so they're easy to test.
import type { Instance, QueryResult } from "../../api";
import { interfaceSettings } from "../pfsense-interfaces/interfaceSettings";

/** Where a port's counters come from. */
export type PortSource = "pfsense" | "snmp" | "node";

export interface PortRef {
  source: PortSource;
  instanceId: string;
  /** The interface as the metric labels name it (igc0, port3, eth0). */
  iface: string;
}

export const MAX_PORTS = 8;
const SETTING_KEYS = Array.from({ length: MAX_PORTS }, (_, i) => `port${i + 1}`);
const SOURCES: readonly PortSource[] = ["pfsense", "snmp", "node"];

const SOURCE_METRICS: Record<PortSource, { in: string; out: string; label: string }> = {
  pfsense: { in: "net_bytes_recv", out: "net_bytes_sent", label: "interface" },
  snmp: {
    in: '__name__=~"vantage_(snmp_device|mikrotik_swos)_if_in_bytes_total"',
    out: '__name__=~"vantage_(snmp_device|mikrotik_swos)_if_out_bytes_total"',
    label: "if_name",
  },
  node: {
    in: "node_network_receive_bytes_total",
    out: "node_network_transmit_bytes_total",
    label: "device",
  },
};

/** One fleet query per source listing its ports (loopback and virtual interfaces left out). */
export const PORT_LIST_QUERIES: Record<PortSource, string> = {
  pfsense:
    'count by (vantage_instance_id, interface) (net_bytes_recv{interface!~"lo.*|pflog.*|pfsync.*|enc.*"})',
  snmp: 'count by (vantage_instance_id, if_name) ({__name__=~"vantage_(snmp_device|mikrotik_swos)_if_in_bytes_total"})',
  node: 'count by (vantage_instance_id, device) (node_network_receive_bytes_total{device!~"lo|veth.*|docker.*|br-.*|virbr.*"})',
};
/** The SNMP info series, for the names set on the device (if_label) and descriptions. */
export const SNMP_INFO_QUERY = '{__name__=~"vantage_(snmp_device|mikrotik_swos)_if_info"}';

export const portKey = (p: PortRef) => `${p.source}:${p.instanceId}:${p.iface}`;

function parsePort(value: unknown): PortRef | null {
  if (typeof value !== "string") return null;
  const first = value.indexOf(":");
  const second = value.indexOf(":", first + 1);
  if (first < 0 || second < 0) return null;
  const source = value.slice(0, first) as PortSource;
  const instanceId = value.slice(first + 1, second);
  const iface = value.slice(second + 1);
  return SOURCES.includes(source) && instanceId && iface ? { source, instanceId, iface } : null;
}

/** The ports chosen in a card's settings. */
export function portsFromSettings(settings: Record<string, unknown>): PortRef[] {
  return SETTING_KEYS.map((k) => parsePort(settings[k])).filter((p): p is PortRef => p !== null);
}

/** Card settings holding `ports` (at most MAX_PORTS), replacing any earlier choice. */
export function settingsWithPorts(
  settings: Record<string, string | number | boolean | null>,
  ports: PortRef[],
): Record<string, string | number | boolean | null> {
  const rest = Object.fromEntries(
    Object.entries(settings).filter(([k]) => !SETTING_KEYS.includes(k)),
  );
  ports.slice(0, MAX_PORTS).forEach((p, i) => (rest[`port${i + 1}`] = portKey(p)));
  return rest;
}

/** A PromQL string literal. Interface names come from devices, so escape them. */
export function promqlString(value: string): string {
  return `"${value.replace(/\\/g, "\\\\").replace(/"/g, '\\"').replace(/\n/g, "\\n")}"`;
}

function selector(metric: string, matchers: string[]): string {
  // `metric` is either a plain name or a `__name__=~...` matcher.
  return metric.startsWith("__name__")
    ? `{${[metric, ...matchers].join(",")}}`
    : `${metric}{${matchers.join(",")}}`;
}

/** Bits per second in (`in`) or out of the chosen ports, summed; 0 when nothing reports. */
export function rateQuery(ports: PortRef[], dir: "in" | "out"): string {
  if (ports.length === 0) return "vector(0)";
  const parts = ports.map((p) => {
    const m = SOURCE_METRICS[p.source];
    const sel = selector(m[dir], [
      `vantage_instance_id=${promqlString(p.instanceId)}`,
      `${m.label}=${promqlString(p.iface)}`,
    ]);
    return `(sum(rate(${sel}[2m])) or vector(0))`;
  });
  return `8 * (${parts.join(" + ")})`;
}

export interface PortOption {
  port: PortRef;
  name: string;
  /** The OS or metric name, when it differs from `name`. */
  detail?: string;
}

/** The ports there are, per instance, named the way the cards name them. */
export function portOptions(
  lists: Partial<Record<PortSource, QueryResult | undefined>>,
  snmpInfo: QueryResult | undefined,
  instances: Instance[],
): { instance: Instance; ports: PortOption[] }[] {
  const byId = new Map(instances.map((i) => [i.id, i]));
  const snmpNames = new Map<string, string>();
  for (const s of snmpInfo?.series ?? []) {
    const { vantage_instance_id: id, if_name: ifName, if_label: label } = s.labels;
    if (id && ifName && label) snmpNames.set(`${id}:${ifName}`, label);
  }
  const grouped = new Map<string, PortOption[]>();
  for (const source of SOURCES) {
    const label = SOURCE_METRICS[source].label;
    for (const s of lists[source]?.series ?? []) {
      const instanceId = s.labels.vantage_instance_id;
      const iface = s.labels[label];
      const instance = instanceId ? byId.get(instanceId) : undefined;
      if (!instanceId || !instance || !iface) continue;
      let name = iface;
      if (source === "pfsense") {
        const settings = interfaceSettings(instance);
        if (settings.hidden(iface)) continue;
        name = settings.label(iface);
      } else if (source === "snmp") {
        name = snmpNames.get(`${instanceId}:${iface}`) || iface;
      }
      const options = grouped.get(instanceId) ?? [];
      options.push({
        port: { source, instanceId, iface },
        name,
        detail: name === iface ? undefined : iface,
      });
      grouped.set(instanceId, options);
    }
  }
  return [...grouped.entries()]
    .map(([id, ports]) => ({
      instance: byId.get(id) as Instance,
      ports: ports.sort((a, b) =>
        a.port.iface.localeCompare(b.port.iface, undefined, { numeric: true }),
      ),
    }))
    .sort((a, b) => a.instance.name.localeCompare(b.instance.name));
}
