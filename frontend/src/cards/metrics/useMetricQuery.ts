import { useQuery } from "@tanstack/react-query";
import type { Instance, QueryResult } from "../../api";
import { useApiClient } from "../../app/session";
import { INSTANCES_KEY } from "../../pages/devices/queries";
import {
  FRESH,
  freshFor,
  hasData,
  HISTORY_SECONDS,
  NO_DATA_31_DAYS,
  NO_RECENT_DATA,
  NO_STORED_HISTORY,
  POLL_INTERVAL_SECONDS,
  SCRAPE_INTERVAL_SECONDS,
  sourceOf,
} from "./freshness";
import type { Freshness } from "./freshness";
import { scopedQuery } from "./promql";
import type { Selector } from "./promql";
import { useScrapeStates } from "./scrapeStatus";

export const INSTANT_REFETCH_MS = 15_000;
export const RANGE_REFETCH_MS = 30_000;
const TARGET_POINTS = 60;
/** The query proxy's minimum step (M5.1). */
const MIN_STEP_SECONDS = 15;

/** The range hook's default window: the last hour. */
export const DEFAULT_WINDOW_SECONDS = 3600;

export interface RangeWindow {
  start: number;
  end: number;
  step: number;
}

/** A window ending now, with about 60 points. `end` is rounded to the step so refetches line up. */
export function rangeWindow(windowSeconds: number, nowMs: number = Date.now()): RangeWindow {
  const step = Math.max(MIN_STEP_SECONDS, Math.ceil(windowSeconds / TARGET_POINTS));
  const end = Math.floor(nowMs / 1000 / step) * step;
  return { start: end - windowSeconds, end, step };
}

/** A past value doesn't change; keep it a while instead of asking again every 15 s. */
const PAST_STALE_MS = 5 * 60_000;
/** Windows searched, shortest first, for a scraped target's last `up == 1`. */
export const LAST_UP_WINDOWS = [3600, 24 * 3600, HISTORY_SECONDS] as const;
/** Points per lookup, under the proxy's 1 100-point limit. */
const LAST_UP_POINTS = 1000;
const EMPTY: QueryResult = { result_type: "matrix", series: [], warnings: [] };

/** The instance from the cached instances list (the dashboard keeps it loaded); never fetches. */
function useCachedInstance(instanceId: string): Instance | undefined {
  const client = useApiClient();
  return useQuery({
    queryKey: INSTANCES_KEY,
    queryFn: () => client.instances(),
    enabled: false,
    select: (list: Instance[]) => list.find((i) => i.id === instanceId),
  }).data;
}

/**
 * When a scraped target last had `up == 1` (Unix seconds), or null if not in stored history.
 * Searches 1 h, then 24 h, then 31 days and stops at the first hit. One cached result per
 * instance, shared by all of a card's queries.
 */
function useLastUp(instanceId: string, enabled: boolean) {
  const client = useApiClient();
  const expr = scopedQuery(instanceId, (sel) => `up${sel()} == 1`);
  return useQuery<number | null>({
    queryKey: ["query", "last-up", expr],
    queryFn: async () => {
      const end = Math.floor(Date.now() / 1000);
      for (const span of LAST_UP_WINDOWS) {
        const step = Math.max(MIN_STEP_SECONDS, Math.ceil(span / LAST_UP_POINTS));
        const result = await client.query({ query: expr, start: end - span, end, step });
        let last: number | null = null;
        for (const s of result.series) {
          for (const [t] of s.values) if (last === null || t > last) last = t;
        }
        if (last !== null) return last;
      }
      return null;
    },
    staleTime: PAST_STALE_MS,
    enabled,
  });
}

/** What `useInstantQuery` returns: the value to show, and how old it is. */
export interface InstantQuery {
  data: QueryResult | undefined;
  isPending: boolean;
  error: Error | null;
  freshness: Freshness;
}

/**
 * Instant query for one instance. `build` gets a selector that always carries
 * `vantage_instance_id`, so the query can't read another instance's series.
 * Refetches every 15 s while the tab is visible.
 *
 * When the live result is empty or the source is stale, the same query text, unchanged, is
 * evaluated at the source's last good moment (a one-point range query) and returned with
 * `freshness.kind === "stale"`. The query is never rewritten (no `last_over_time`): evaluated
 * at that moment, it gives exactly what the card showed then. The instance (and its
 * `last_success_at`) comes from the cached instances list.
 */
export function useInstantQuery(
  instanceId: string,
  build: (sel: Selector) => string,
): InstantQuery {
  const client = useApiClient();
  const expr = scopedQuery(instanceId, build);
  const instance = useCachedInstance(instanceId);
  const scrapeState = useScrapeStates([instance]).get(instanceId);
  const live = useQuery<QueryResult>({
    queryKey: ["query", "instant", expr],
    queryFn: () => client.query({ query: expr }),
    refetchInterval: INSTANT_REFETCH_MS,
  });

  // "Now" is when the live query last answered (every 15 s), which keeps the render pure.
  const now = Math.max(live.dataUpdatedAt, live.errorUpdatedAt) / 1000;
  const source = sourceOf(instance);
  const liveEmpty = live.isSuccess && !hasData(live.data);
  // The moment to evaluate the query at instead of now, or why there is nothing to show.
  let at: number | null = null;
  let missing: string | null = null;
  if (source.kind === "polled") {
    const last = source.lastSuccess;
    if (last === null) {
      // A new device (pending) keeps the card's own "no data yet" hint.
      if (liveEmpty && !source.pending) missing = NO_RECENT_DATA;
    } else if (now - last > freshFor(POLL_INTERVAL_SECONDS) || liveEmpty) {
      if (now - last > HISTORY_SECONDS) missing = NO_DATA_31_DAYS;
      else at = last;
    }
  }
  // A scraped target whose `up` is 1 now is fresh; otherwise find its last good scrape.
  const lookUp =
    source.kind === "scraped" && scrapeState !== "up" && (scrapeState === "down" || liveEmpty);
  const lastUp = useLastUp(instanceId, lookUp);
  if (lookUp && lastUp.isSuccess) {
    if (lastUp.data === null) missing = NO_STORED_HISTORY;
    else if (now - lastUp.data > freshFor(SCRAPE_INTERVAL_SECONDS) || liveEmpty) at = lastUp.data;
  }

  const past = useQuery<QueryResult>({
    queryKey: ["query", "at", expr, at],
    queryFn: () =>
      client.query({ query: expr, start: at ?? 0, end: at ?? 0, step: MIN_STEP_SECONDS }),
    staleTime: PAST_STALE_MS,
    enabled: at !== null,
  });

  const error = live.error ?? (lookUp ? lastUp.error : null) ?? (at !== null ? past.error : null);
  if (error) return { data: undefined, isPending: false, error, freshness: FRESH };
  if (missing !== null) {
    return {
      data: EMPTY,
      isPending: false,
      error: null,
      freshness: { kind: "missing", note: missing },
    };
  }
  if (lookUp && lastUp.isPending) {
    return { data: undefined, isPending: true, error: null, freshness: FRESH };
  }
  if (at === null)
    return { data: live.data, isPending: live.isPending, error: null, freshness: FRESH };
  if (past.isPending) return { data: undefined, isPending: true, error: null, freshness: FRESH };
  if (hasData(past.data)) {
    return {
      data: past.data,
      isPending: false,
      error: null,
      freshness: { kind: "stale", asOf: at },
    };
  }
  const freshness: Freshness = { kind: "missing", note: NO_RECENT_DATA };
  return { data: EMPTY, isPending: false, error: null, freshness };
}

/** Range query over the last `windowSeconds` (default 1 h) for one instance; refetches every 30 s. */
export function useRangeQuery(
  instanceId: string,
  build: (sel: Selector) => string,
  windowSeconds: number = DEFAULT_WINDOW_SECONDS,
) {
  const client = useApiClient();
  const expr = scopedQuery(instanceId, build);
  return useQuery<QueryResult>({
    queryKey: ["query", "range", expr, windowSeconds],
    queryFn: () => client.query({ query: expr, ...rangeWindow(windowSeconds) }),
    refetchInterval: RANGE_REFETCH_MS,
  });
}

/**
 * Instant query across all instances (overview row). Unlike the hooks above this is not scoped
 * to one instance, so `expr` must be a fixed string written in code, never built from user input.
 * Live data only: it never falls back to past values.
 */
export function useFleetQuery(expr: string) {
  const client = useApiClient();
  return useQuery<QueryResult>({
    queryKey: ["query", "fleet", expr],
    queryFn: () => client.query({ query: expr }),
    refetchInterval: INSTANT_REFETCH_MS,
  });
}

/** Range query over the whole fleet (dashboard-wide cards); refetches every 30 s. */
export function useFleetRangeQuery(
  expr: string,
  windowSeconds: number = DEFAULT_WINDOW_SECONDS,
  enabled = true,
) {
  const client = useApiClient();
  return useQuery<QueryResult>({
    queryKey: ["query", "fleet-range", expr, windowSeconds],
    queryFn: () => client.query({ query: expr, ...rangeWindow(windowSeconds) }),
    refetchInterval: RANGE_REFETCH_MS,
    enabled,
  });
}
