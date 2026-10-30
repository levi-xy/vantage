// Fleet-wide numbers for the overview row. Pure functions, so they're easy to test.

import type { Instance, QueryResult } from "../api";
import { parseValue } from "./metrics/promql";

export interface HealthCounts {
  up: number;
  down: number;
  pending: number;
  unknown: number;
}

/** `state` gives the state to count (the backend's, or Prometheus' for unpolled devices). */
export function healthCounts(
  instances: Instance[],
  state: (i: Instance) => Instance["status"]["state"] = (i) => i.status.state,
): HealthCounts {
  const counts: HealthCounts = { up: 0, down: 0, pending: 0, unknown: 0 };
  for (const i of instances) counts[state(i)] += 1;
  return counts;
}

/**
 * WAN sources. To add another one, extend these two: the type id, and a query that returns one
 * series per uplink with a value of 100 when it is down.
 * pfSense (Telegraf): one `ping_percent_packet_loss` series per gateway. Loss, not RTT, tells a
 * down gateway (a down gateway has no RTT series at all).
 */
export const WAN_TYPE_IDS: readonly string[] = ["pfsense"];
export const WAN_LOSS_QUERY = 'ping_percent_packet_loss{vantage_integration="pfsense"}';

export function hasWanSource(instances: Instance[]): boolean {
  return instances.some((i) => WAN_TYPE_IDS.includes(i.type_id));
}

/** A gateway is up unless its loss is 100 %. */
export function wanSummary(result: QueryResult | undefined): { up: number; total: number } {
  const series = result?.series ?? [];
  let up = 0;
  for (const s of series) {
    const loss = parseValue(s.values[0]?.[1]);
    if (loss !== undefined && loss < 100) up += 1;
  }
  return { up, total: series.length };
}

/**
 * Ballpark total throughput, in bits per second: rx + tx summed over every instance that has
 * them: node_exporter interface rates (loopback and virtual NICs excluded) and SNMP interface
 * rates (SNMP devices and SwOS switches). pfSense (`net_bytes_*`) is not included yet. SNMP interfaces are not filtered, so a
 * device that counts both a bridge and its member ports is counted twice. Reads 0 when no
 * source has data.
 */
const REAL_NIC = 'device!~"lo|veth.*|docker.*|br-.*|virbr.*"';
const rate = (selector: string) => `(sum(rate(${selector}[5m])) or vector(0))`;
const SNMP_IF = (dir: "in" | "out") =>
  `{__name__=~"vantage_(snmp_device|mikrotik_swos)_if_${dir}_bytes_total"}`;
export const THROUGHPUT_QUERY =
  `8 * (${rate(`node_network_receive_bytes_total{${REAL_NIC}}`)}` +
  ` + ${rate(`node_network_transmit_bytes_total{${REAL_NIC}}`)}` +
  ` + ${rate(SNMP_IF("in"))} + ${rate(SNMP_IF("out"))})`;
