// Status for devices the collector doesn't poll (scrape-only integrations such as pfSense):
// their backend status is "unknown", but Prometheus knows whether scraping them works.
import { useQuery } from "@tanstack/react-query";
import type { Instance, InstanceStatus, QueryResult } from "../../api";
import { useApiClient } from "../../app/session";
import { parseValue } from "./promql";

const SCRAPE_UP_QUERY = 'max by (vantage_instance_id) (up{vantage_instance_id!=""})';
const REFETCH_MS = 30_000;

type State = InstanceStatus["state"];

/** Scrape state by instance id: "up" when any of its targets is up, else "down". */
export function scrapeStates(result: QueryResult | undefined): Map<string, State> {
  const states = new Map<string, State>();
  for (const s of result?.series ?? []) {
    const id = s.labels.vantage_instance_id;
    if (id) states.set(id, parseValue(s.values[0]?.[1]) === 1 ? "up" : "down");
  }
  return states;
}

const needsScrapeState = (i: Instance | undefined) =>
  i !== undefined && i.enabled && i.status.state === "unknown";

/** One shared query (deduplicated across cards), only run when some instance needs it. */
export function useScrapeStates(instances: (Instance | undefined)[]): Map<string, State> {
  const client = useApiClient();
  const query = useQuery<QueryResult>({
    queryKey: ["query", "fleet", SCRAPE_UP_QUERY],
    queryFn: () => client.query({ query: SCRAPE_UP_QUERY }),
    refetchInterval: REFETCH_MS,
    enabled: instances.some(needsScrapeState),
  });
  return scrapeStates(query.data);
}

/** The state to show: the backend's, or for an unpolled instance, Prometheus' scrape state. */
export function shownState(instance: Instance | undefined, scrape: Map<string, State>): State {
  if (!instance) return "unknown";
  if (!needsScrapeState(instance)) return instance.status.state;
  return scrape.get(instance.id) ?? "unknown";
}
