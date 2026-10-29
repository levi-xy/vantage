import { useQuery } from "@tanstack/react-query";
import type { QueryResult } from "../../api";
import { useApiClient } from "../../app/session";
import { scopedQuery } from "./promql";
import type { Selector } from "./promql";

export const INSTANT_REFETCH_MS = 15_000;
export const RANGE_REFETCH_MS = 30_000;
const TARGET_POINTS = 60;

/** The range hook's default window: the last hour. */
export const DEFAULT_WINDOW_SECONDS = 3600;

export interface RangeWindow {
  start: number;
  end: number;
  step: number;
}

/** A window ending now, with about 60 points. `end` is rounded to the step so refetches line up. */
export function rangeWindow(windowSeconds: number, nowMs: number = Date.now()): RangeWindow {
  const step = Math.max(1, Math.ceil(windowSeconds / TARGET_POINTS));
  const end = Math.floor(nowMs / 1000 / step) * step;
  return { start: end - windowSeconds, end, step };
}

/**
 * Instant query for one instance. `build` gets a selector that always carries
 * `vantage_instance_id`, so the query can't read another instance's series.
 * Refetches every 15 s while the tab is visible.
 */
export function useInstantQuery(instanceId: string, build: (sel: Selector) => string) {
  const client = useApiClient();
  const expr = scopedQuery(instanceId, build);
  return useQuery<QueryResult>({
    queryKey: ["query", "instant", expr],
    queryFn: () => client.query({ query: expr }),
    refetchInterval: INSTANT_REFETCH_MS,
  });
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
