import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router";
import type { ReactNode } from "react";
import { StatTile } from "./metrics/StatTile";
import type { StatTileProps } from "./metrics/StatTile";
import { shownState, useScrapeStates } from "./metrics/scrapeStatus";
import { useApiClient } from "../app/session";
import { INSTANT_REFETCH_MS, useFleetQuery } from "./metrics/useMetricQuery";
import {
  hasWanSource,
  healthCounts,
  WAN_LOSS_QUERY,
  wanSummary,
  withoutFreshData,
} from "./overview";
import type { WidgetProps } from "./widgets";
import { INSTANCES_KEY } from "../pages/devices/queries";

function Stat({ note, children }: { note?: string; children: ReactNode }) {
  return (
    <div className="flex h-full min-w-0 flex-col justify-center p-4">
      {children}
      {note && <p className="mt-0.5 text-xs text-slate-500 dark:text-slate-400">{note}</p>}
    </div>
  );
}

export function DevicesHealthCard({ instances }: WidgetProps) {
  const client = useApiClient();
  const scrape = useScrapeStates(instances);
  const counts = healthCounts(instances, (i) => shownState(i, scrape));
  const polled = counts.up + counts.down + counts.pending;
  // Judged as of the list's last update (refetch or `/ws` push), not render time: pure, and a
  // list that lags behind can't count a device as stale that isn't.
  const listAt = useQuery({
    queryKey: INSTANCES_KEY,
    queryFn: () => client.instances(),
    enabled: false,
  }).dataUpdatedAt;
  const notFresh = listAt ? withoutFreshData(instances, scrape, listAt) : 0;
  const notes: string[] = [];
  if (counts.down) notes.push(`${counts.down} down`);
  else if (counts.pending) notes.push(`${counts.pending} pending`);
  if (notFresh)
    notes.push(`${notFresh} ${notFresh === 1 ? "device" : "devices"} without fresh data`);
  const note = notes.length ? notes.join(" · ") : undefined;
  return (
    <Stat note={note}>
      <Link to="/devices" className="block">
        <StatTile
          label="Devices healthy"
          value={polled ? `${counts.up} of ${polled}` : "—"}
          tone={counts.down ? "bad" : polled ? "ok" : undefined}
        />
      </Link>
    </Stat>
  );
}

const FIRING_LIMIT = 200;

export function AlertsCard() {
  const client = useApiClient();
  const firing = useQuery({
    queryKey: ["alerts", "open"],
    queryFn: () => client.openAlertEvents(FIRING_LIMIT),
    refetchInterval: INSTANT_REFETCH_MS,
  });
  const count = firing.data?.length;
  const value =
    count === undefined ? "\u2014" : count >= FIRING_LIMIT ? `${count}+` : String(count);
  return (
    <Stat note={count === 0 ? "All clear" : undefined}>
      <StatTile label="Active alerts" value={value} tone={count ? "bad" : undefined} />
    </Stat>
  );
}

export function WanStatusCard({ instances }: WidgetProps) {
  const wan = useFleetQuery(WAN_LOSS_QUERY);
  const state = wanSummary(wan.data);
  let value = "No WAN source";
  let tone: StatTileProps["tone"];
  if (hasWanSource(instances)) {
    if (wan.isError || !state.total) {
      value = "—";
    } else {
      value = `${state.up} of ${state.total} gateways up`;
      tone = state.up === state.total ? "ok" : "bad";
    }
  }
  return (
    <Stat>
      <StatTile label="WAN status" value={value} tone={tone} />
    </Stat>
  );
}
