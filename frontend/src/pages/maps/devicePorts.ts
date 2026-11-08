// A linked device's own interfaces for the map: state and traffic per port, from a few
// combined fleet queries (never one per box or per cable). SNMP switches report link state,
// admin state, speed and counters; pfSense and Linux hosts report counters (Linux also the
// link flag). The queries are the ones the port cards already use.

import { useQueries } from "@tanstack/react-query";
import type { Instance, QueryResult } from "../../api";
import { useApiClient } from "../../app/session";
import { INSTANT_REFETCH_MS } from "../../cards/metrics/useMetricQuery";
import { parseValue } from "../../cards/metrics/promql";
import { REAL_IF } from "../../cards/pfsense-interfaces/interfaceSettings";
import { IF_DOWN, IF_UP } from "../../cards/snmp-overview/snmpMetrics";
import { isInternal } from "../../cards/snmp-ports/ports";
import type { PortState } from "../../cards/snmp-ports/ports";
import type { PortRef, PortSource } from "../../cards/throughput/ports";
import { useDocumentHidden } from "./useDocumentHidden";

export interface DevicePort {
  /** The interface as the metrics name it (port3, igc0, eth0): what a cable's `port` holds. */
  id: string;
  /** The name set on the device, for tooltips. */
  name: string;
  /** Null when the integration doesn't report a link state. */
  state: PortState | null;
  speed?: number;
  inBps?: number;
  outBps?: number;
  /** Where the counters are, for the last-hour chart; null without counters. */
  ref: PortRef | null;
  order: number;
}

export type DevicePorts = ReadonlyMap<string, DevicePort[]>;

const SNMP = '__name__=~"vantage_(snmp_device|mikrotik_swos)_';
const NODE_NIC = 'device!~"lo|veth.*|docker.*|br-.*|virbr.*"';

const SOURCE_OF: Record<string, PortSource> = {
  snmp_device: "snmp",
  mikrotik_swos: "snmp",
  pfsense: "pfsense",
  linux_host: "node",
};

/** Whether the map can read this device's interfaces. */
export const hasPorts = (instance: Instance | undefined): boolean =>
  instance !== undefined && instance.type_id in SOURCE_OF;

// Fixed query strings; the keys below say which part of the answer each one is.
const QUERIES = [
  ["snmp", "oper", `{${SNMP}if_oper_status"}`],
  ["snmp", "admin", `{${SNMP}if_admin_status"}`],
  ["snmp", "speed", `{${SNMP}if_speed_bits_per_second"}`],
  ["snmp", "rx", `8 * rate({${SNMP}if_in_bytes_total"}[5m])`],
  ["snmp", "tx", `8 * rate({${SNMP}if_out_bytes_total"}[5m])`],
  ["snmp", "info", `{${SNMP}if_info"}`],
  ["pfsense", "rx", `8 * rate(net_bytes_recv{${REAL_IF}}[5m])`],
  ["pfsense", "tx", `8 * rate(net_bytes_sent{${REAL_IF}}[5m])`],
  ["node", "rx", `8 * rate(node_network_receive_bytes_total{${NODE_NIC}}[5m])`],
  ["node", "tx", `8 * rate(node_network_transmit_bytes_total{${NODE_NIC}}[5m])`],
  ["node", "up", `node_network_up{${NODE_NIC}}`],
] as const;

type Row = { instanceId: string; name: string; labels: Record<string, string>; value?: number };

function rows(result: QueryResult | undefined, label: string): Row[] {
  const out: Row[] = [];
  for (const s of result?.series ?? []) {
    const instanceId = s.labels.vantage_instance_id;
    const name = s.labels[label];
    if (instanceId && name) {
      out.push({ instanceId, name, labels: s.labels, value: parseValue(s.values[0]?.[1]) });
    }
  }
  return out;
}

const keyOf = (r: { instanceId: string; name: string }) => `${r.instanceId}\u0000${r.name}`;
const valuesOf = (list: Row[]) => new Map(list.map((r) => [keyOf(r), r.value] as const));

function build(results: Map<string, QueryResult | undefined>): DevicePorts {
  const get = (src: string, part: string, label: string) =>
    rows(results.get(`${src}.${part}`), label);
  const out = new Map<string, DevicePort[]>();
  const add = (instanceId: string, port: DevicePort) =>
    out.set(instanceId, [...(out.get(instanceId) ?? []), port]);

  // SNMP: the list of ports is the oper-status series.
  const admin = valuesOf(get("snmp", "admin", "if_name"));
  const speed = valuesOf(get("snmp", "speed", "if_name"));
  const rx = valuesOf(get("snmp", "rx", "if_name"));
  const tx = valuesOf(get("snmp", "tx", "if_name"));
  const labels = new Map(get("snmp", "info", "if_name").map((r) => [keyOf(r), r.labels] as const));
  for (const r of get("snmp", "oper", "if_name")) {
    const k = keyOf(r);
    const name = labels.get(k)?.if_label || r.name;
    if (isInternal({ id: r.name, name })) continue;
    add(r.instanceId, {
      id: r.name,
      name,
      state: admin.get(k) === IF_DOWN ? "disabled" : r.value === IF_UP ? "up" : "down",
      speed: speed.get(k),
      inBps: rx.get(k),
      outBps: tx.get(k),
      ref: { source: "snmp", instanceId: r.instanceId, iface: r.name },
      order: Number(r.labels.if_index) || 0,
    });
  }

  // pfSense and Linux: the list is the receive counters; Linux adds the link flag.
  const counters = (src: PortSource, label: string, up?: Map<string, number | undefined>) => {
    const sent = valuesOf(get(src, "tx", label));
    get(src, "rx", label).forEach((r, order) => {
      const flag = up?.get(keyOf(r));
      add(r.instanceId, {
        id: r.name,
        name: r.name,
        state: flag === undefined ? null : flag === 1 ? "up" : "down",
        inBps: r.value,
        outBps: sent.get(keyOf(r)),
        ref: { source: src, instanceId: r.instanceId, iface: r.name },
        order,
      });
    });
  };
  counters("pfsense", "interface");
  counters("node", "device", valuesOf(get("node", "up", "device")));

  for (const list of out.values()) list.sort((a, b) => a.order - b.order);
  return out;
}

/**
 * The interfaces of the given linked devices. Only the sources in use are queried; each
 * query covers the whole fleet once, whatever the number of boxes or cables.
 */
export function useDevicePorts(devices: readonly Instance[]): DevicePorts {
  const client = useApiClient();
  const hidden = useDocumentHidden();
  const sources = new Set(devices.map((d) => SOURCE_OF[d.type_id]));
  const active = QUERIES.filter(([src]) => sources.has(src));
  return useQueries({
    queries: active.map(([, , expr]) => ({
      queryKey: ["query", "fleet", expr],
      queryFn: () => client.query({ query: expr }),
      refetchInterval: hidden ? false : INSTANT_REFETCH_MS,
    })),
    combine: (res) =>
      build(new Map(active.map(([src, part], i) => [`${src}.${part}`, res[i]?.data] as const))),
  });
}
