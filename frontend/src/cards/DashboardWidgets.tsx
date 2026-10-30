import { Link } from "react-router";
import type { ReactNode } from "react";
import { StatTile } from "./metrics/StatTile";
import type { StatTileProps } from "./metrics/StatTile";
import { shownState, useScrapeStates } from "./metrics/scrapeStatus";
import { useFleetQuery } from "./metrics/useMetricQuery";
import { hasWanSource, healthCounts, WAN_LOSS_QUERY, wanSummary } from "./overview";
import type { WidgetProps } from "./widgets";

function Stat({ note, children }: { note?: string; children: ReactNode }) {
  return (
    <div className="flex h-full min-w-0 flex-col justify-center p-4">
      {children}
      {note && <p className="mt-0.5 text-xs text-slate-500 dark:text-slate-400">{note}</p>}
    </div>
  );
}

export function DevicesHealthCard({ instances }: WidgetProps) {
  const scrape = useScrapeStates(instances);
  const counts = healthCounts(instances, (i) => shownState(i, scrape));
  const polled = counts.up + counts.down + counts.pending;
  let note: string | undefined;
  if (counts.down) note = `${counts.down} down`;
  else if (counts.pending) note = `${counts.pending} pending`;
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

export function AlertsCard() {
  // TODO(M7): show the number of firing alerts once the alerts API exists.
  return (
    <Stat note="Alerts not set up yet">
      <StatTile label="Active alerts" value="—" />
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
