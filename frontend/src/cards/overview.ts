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
