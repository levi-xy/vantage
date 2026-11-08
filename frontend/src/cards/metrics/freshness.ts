// Freshness: how old the value on screen is. Separate from availability (the status badge).
// A source is fresh while its last observation is younger than 2 x its interval + 30 s.

import { createContext } from "react";
import type { Instance, QueryResult } from "../../api";
import { formatClockOrDay } from "../../app/time";

/**
 * The API doesn't tell the frontend an instance's poll interval, so polled instances use the
 * default poll interval and scraped targets Prometheus' scrape interval.
 */
export const POLL_INTERVAL_SECONDS = 60;
export const SCRAPE_INTERVAL_SECONDS = 30;
export const freshFor = (intervalSeconds: number) => 2 * intervalSeconds + 30;

/** How far back the query proxy lets a range query reach (MAX_RANGE_SECONDS, 31 days). */
export const HISTORY_SECONDS = 31 * 24 * 3600;

export const NO_RECENT_DATA = "No recent data.";
export const NO_DATA_31_DAYS = "No data in the last 31 days.";
export const NO_STORED_HISTORY = "No data in stored history.";

export type Freshness =
  | { kind: "fresh" }
  /** The value shown is the one at `asOf` (Unix seconds). */
  | { kind: "stale"; asOf: number }
  /** Nothing to show, and why. */
  | { kind: "missing"; note: string };

export const FRESH: Freshness = { kind: "fresh" };

/** How a card's source is observed: polled by the collector, or scraped by Prometheus. */
export type Source =
  | { kind: "unknown" }
  | { kind: "polled"; lastSuccess: number | null; pending: boolean }
  | { kind: "scraped" };

export function sourceOf(instance: Instance | undefined): Source {
  if (!instance) return { kind: "unknown" };
  const { state, last_success_at } = instance.status;
  const lastSuccess = last_success_at ? Date.parse(last_success_at) / 1000 : NaN;
  if (Number.isFinite(lastSuccess)) return { kind: "polled", lastSuccess, pending: false };
  // `unknown` with no success on record: the collector doesn't poll it (scrape-only).
  if (state === "unknown") return { kind: "scraped" };
  return { kind: "polled", lastSuccess: null, pending: state === "pending" };
}

export const hasData = (r: QueryResult | undefined) => (r?.series.length ?? 0) > 0;

/** One freshness for a card from its queries: the oldest stale value wins, then a note. */
export function cardFreshness(...queries: { freshness: Freshness }[]): Freshness {
  let out: Freshness = FRESH;
  for (const { freshness: f } of queries) {
    if (f.kind === "stale") {
      if (out.kind !== "stale" || f.asOf < out.asOf) out = f;
    } else if (f.kind === "missing" && out.kind === "fresh") {
      out = f;
    }
  }
  return out;
}

/** "14:05", with the date when it isn't today. */
export function formatAsOf(unixSeconds: number, now: Date = new Date()): string {
  return formatClockOrDay(unixSeconds * 1000, now.getTime());
}

/** True inside a card body that shows a past value: threshold colours turn neutral. */
export const StaleContext = createContext(false);
