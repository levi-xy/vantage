import { Link } from "react-router-dom";
import type { ReactNode } from "react";
import type { Instance } from "../api";
import { formatBitsPerSecond } from "./metrics/format";
import { firstValue } from "./metrics/promql";
import { StatTile } from "./metrics/StatTile";
import type { StatTileProps } from "./metrics/StatTile";
import { useFleetQuery } from "./metrics/useMetricQuery";
import {
  hasWanSource,
  healthCounts,
  THROUGHPUT_QUERY,
  WAN_LOSS_QUERY,
  wanSummary,
} from "./overview";

function Stat({ note, children }: { note?: string; children: ReactNode }) {
  return (
    <div className="min-w-0 rounded-lg border border-slate-200 bg-white p-3 dark:border-slate-800 dark:bg-slate-900">
      {children}
      {note && <p className="mt-0.5 text-xs text-slate-500 dark:text-slate-400">{note}</p>}
    </div>
  );
}

/**
 * Fixed row above the grid (not part of the saved layout): devices healthy, active alerts,
 * WAN status, total throughput.
 */
export function OverviewRow({ instances }: { instances: Instance[] }) {
  const counts = healthCounts(instances);
  const polled = counts.up + counts.down + counts.pending;
  const wanSource = hasWanSource(instances);
  const wan = useFleetQuery(WAN_LOSS_QUERY);
  const throughput = useFleetQuery(THROUGHPUT_QUERY);

  const wanState = wanSummary(wan.data);
  let wanValue = "No WAN source";
  let wanTone: StatTileProps["tone"];
  if (wanSource) {
    if (wan.isError || !wanState.total) {
      wanValue = "—";
    } else {
      wanValue = `${wanState.up} of ${wanState.total} gateways up`;
      wanTone = wanState.up === wanState.total ? "ok" : "bad";
    }
  }

  let healthNote: string | undefined;
  if (counts.down) healthNote = `${counts.down} down`;
  else if (counts.pending) healthNote = `${counts.pending} pending`;

  return (
    <section aria-label="Overview" className="mb-4 grid grid-cols-2 gap-3 lg:grid-cols-4">
      <Link to="/devices" className="block min-w-0">
        <Stat note={healthNote}>
          <StatTile
            label="Devices healthy"
            value={polled ? `${counts.up} of ${polled}` : "—"}
            tone={counts.down ? "bad" : polled ? "ok" : undefined}
          />
        </Stat>
      </Link>
      {/* TODO(M6): show the number of firing alerts once the alerts API exists. */}
      <Stat note="Alerts not set up yet">
        <StatTile label="Active alerts" value="—" />
      </Stat>
      <Stat>
        <StatTile label="WAN status" value={wanValue} tone={wanTone} />
      </Stat>
      <Stat note="Hosts and SNMP devices">
        <StatTile
          label="Total throughput"
          value={throughput.isError ? "—" : formatBitsPerSecond(firstValue(throughput.data))}
        />
      </Stat>
    </section>
  );
}
