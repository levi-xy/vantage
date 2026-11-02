import { useQuery } from "@tanstack/react-query";
import { KpiTile } from "./metrics/KpiTile";
import type { KpiTileProps } from "./metrics/KpiTile";
import { formatMs } from "./metrics/format";
import { shownState, useScrapeStatus } from "./metrics/scrapeStatus";
import { useApiClient } from "../app/session";
import { useFleetQuery } from "./metrics/useMetricQuery";
import { OPEN_ALERTS_LIMIT, useOpenAlerts } from "./openAlerts";
import {
  gateways,
  hasWanSource,
  healthCounts,
  WAN_LOSS_QUERY,
  WAN_RTT_QUERY,
  wanSummary,
  withoutFreshData,
} from "./overview";
import { ageText } from "./problems";
import type { WidgetProps } from "./widgets";
import { INSTANCES_KEY } from "../pages/devices/queries";

export function DevicesHealthCard({ instances }: WidgetProps) {
  const client = useApiClient();
  const scrapeStatus = useScrapeStatus(instances);
  const scrape = scrapeStatus.states;
  const state = (i: (typeof instances)[number]) => shownState(i, scrape);
  const counts = healthCounts(instances, state);
  const polled = counts.up + counts.down + counts.pending;
  // Judged as of the list's last update (refetch or `/ws` push), not render time: pure, and a
  // list that lags behind can't count a device as stale that isn't.
  const listAt = useQuery({
    queryKey: INSTANCES_KEY,
    queryFn: () => client.instances(),
    enabled: false,
  }).dataUpdatedAt;
  const notFresh = listAt ? withoutFreshData(instances, scrape, listAt) : 0;
  let note: string;
  if (counts.down) {
    const names = instances.filter((i) => state(i) === "down").map((i) => i.name);
    note = `${counts.down} down: ${names.join(", ")}`;
  } else if (scrapeStatus.failed) {
    note = `${scrapeStatus.needed} unknown: Prometheus can't be reached`;
  } else if (notFresh) {
    note = `${notFresh} ${notFresh === 1 ? "device" : "devices"} without fresh data`;
  } else if (counts.pending) {
    note = `${counts.pending} pending`;
  } else {
    note = polled
      ? instances
          .filter((i) => state(i) === "up")
          .map((i) => i.name)
          .join(", ")
      : "No device polled yet";
  }
  return (
    <KpiTile
      label="Devices"
      value={polled ? `${counts.up}/${polled}` : "—"}
      unit={polled ? "up" : undefined}
      note={note}
      tone={counts.down ? "bad" : undefined}
      to="/devices"
    />
  );
}

export function AlertsCard() {
  const open = useOpenAlerts();
  const events = open.data;
  if (!events) {
    return (
      <KpiTile
        label="Active alerts"
        value="—"
        note={open.isError ? "Can't load alerts" : undefined}
        to="/alerts"
      />
    );
  }
  const count = events.length;
  // Judged as of the last fetch (pure); the query refetches every 15 s.
  const oldest = Math.min(...events.map((e) => Date.parse(e.started_at)));
  return (
    <KpiTile
      label="Active alerts"
      value={count >= OPEN_ALERTS_LIMIT ? `${count}+` : String(count)}
      unit={count ? (events.every((e) => e.state === "firing") ? "firing" : "open") : undefined}
      note={
        count === 0
          ? "All clear"
          : Number.isFinite(oldest)
            ? `oldest ${ageText(open.dataUpdatedAt - oldest)}`
            : undefined
      }
      tone={count ? "bad" : undefined}
      to="/alerts"
    />
  );
}

export function WanStatusCard({ instances }: WidgetProps) {
  const wan = hasWanSource(instances);
  const loss = useFleetQuery(WAN_LOSS_QUERY);
  const rtt = useFleetQuery(WAN_RTT_QUERY);
  const state = wanSummary(loss.data);
  const tile: KpiTileProps = { label: "Internet", value: "—" };
  if (!wan) {
    tile.note = "No WAN source";
  } else if (loss.isError) {
    tile.note = "Can't read the gateways";
  } else if (!state.total) {
    tile.note = loss.isPending ? undefined : "No gateway data yet";
  } else {
    if (state.up === state.total) tile.value = "Up";
    else if (state.up === 0) tile.value = "Down";
    else {
      tile.value = `${state.up}/${state.total}`;
      tile.unit = "gateways up";
    }
    if (state.up < state.total) tile.tone = "bad";
    const first = gateways(loss.data, rtt.data)[0];
    if (first) {
      const parts = [first.url];
      if (first.lossPct < 100 && first.rttMs !== undefined)
        parts.push(formatMs(first.rttMs / 1000));
      parts.push(`${Number(first.lossPct.toFixed(1))}% loss`);
      tile.note = parts.join(" · ");
    }
  }
  return <KpiTile {...tile} />;
}
