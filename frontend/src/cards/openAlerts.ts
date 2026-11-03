import { useQuery } from "@tanstack/react-query";
import { useApiClient } from "../app/session";
import { ALERTS_KEY } from "../app/live";
import { INSTANT_REFETCH_MS } from "./metrics/useMetricQuery";

/** At most this many of each open state are fetched. */
export const OPEN_ALERTS_LIMIT = 200;

/**
 * Open alert events (firing and no data), newest first. One query shared by the health status
 * and the Active alerts card; `/ws` alert pushes invalidate it (it is keyed under ALERTS_KEY).
 */
export function useOpenAlerts() {
  const client = useApiClient();
  return useQuery({
    queryKey: [...ALERTS_KEY, "open"],
    queryFn: () => client.openAlertEvents(OPEN_ALERTS_LIMIT),
    refetchInterval: INSTANT_REFETCH_MS,
  });
}
