// Fleet-wide numbers for the overview row. Pure functions, so they're easy to test.

import type { Instance, QueryResult } from "../api";
import { freshFor, POLL_INTERVAL_SECONDS, sourceOf } from "./metrics/freshness";
import { parseValue } from "./metrics/promql";

/**
 * Enabled devices whose data isn't fresh: polled ones without a success in the last
 * 2 x interval + 30 s, scraped ones whose target is down. New (pending) devices don't count.
 */
export function withoutFreshData(
  instances: Instance[],
  scrape: Map<string, Instance["status"]["state"]>,
  nowMs: number,
): number {
  let count = 0;
  for (const i of instances) {
    if (!i.enabled) continue;
    const source = sourceOf(i);
    if (source.kind === "scraped") {
      if (scrape.get(i.id) === "down") count += 1;
    } else if (source.kind === "polled") {
      const last = source.lastSuccess;
      if (last === null ? !source.pending : nowMs / 1000 - last > freshFor(POLL_INTERVAL_SECONDS))
        count += 1;
    }
  }
  return count;
}

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

/** Average RTT per gateway, in milliseconds (same `url` label as the loss series). */
export const WAN_RTT_QUERY = 'ping_average_response_ms{vantage_integration="pfsense"}';

export interface Gateway {
  url: string;
  lossPct: number;
  rttMs: number | undefined;
}

/** Gateways by `url`, any down one first, so the Internet tile names it. */
export function gateways(loss: QueryResult | undefined, rtt: QueryResult | undefined): Gateway[] {
  const rttBy = new Map<string, number | undefined>();
  for (const s of rtt?.series ?? []) rttBy.set(s.labels.url ?? "", parseValue(s.values[0]?.[1]));
  const list: Gateway[] = [];
  for (const s of loss?.series ?? []) {
    const lossPct = parseValue(s.values[0]?.[1]);
    if (lossPct === undefined) continue;
    const url = s.labels.url ?? "";
    list.push({ url, lossPct, rttMs: rttBy.get(url) });
  }
  return list.sort(
    (a, b) => Number(b.lossPct >= 100) - Number(a.lossPct >= 100) || a.url.localeCompare(b.url),
  );
}
