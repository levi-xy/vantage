import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { LIVE_RATE_KEY } from "../../app/live";
import type { LiveRate } from "../../app/live";
import { useUnits } from "../../app/units";
import { AreaChart } from "../metrics/Charts";
import { freshFor, POLL_INTERVAL_SECONDS, sourceOf, StaleContext } from "../metrics/freshness";
import { firstSeries, firstValue } from "../metrics/promql";
import { shownState, useScrapeStates } from "../metrics/scrapeStatus";
import { useFleetQuery, useFleetRangeQuery } from "../metrics/useMetricQuery";
import type { WidgetProps, WidgetSettingsProps } from "../widgets";
import {
  MAX_PORTS,
  PORT_LIST_QUERIES,
  portKey,
  portOptions,
  portsFromSettings,
  isWanPort,
  rateQuery,
  settingsWithPorts,
  SNMP_INFO_QUERY,
} from "./ports";
import type { PortRef } from "./ports";
import type { Instance } from "../../api";

/** Every port Vantage has traffic counters for, named as the cards name them. */
export function usePortOptions(instances: Instance[]) {
  const pfsense = useFleetQuery(PORT_LIST_QUERIES.pfsense);
  const snmp = useFleetQuery(PORT_LIST_QUERIES.snmp);
  const node = useFleetQuery(PORT_LIST_QUERIES.node);
  const info = useFleetQuery(SNMP_INFO_QUERY);
  return {
    groups: portOptions(
      { pfsense: pfsense.data, snmp: snmp.data, node: node.data },
      info.data,
      instances,
    ),
    loading: pfsense.isPending || snmp.isPending || node.isPending,
  };
}

// A pushed live rate older than this is stale; the card falls back to Prometheus.
const LIVE_MAX_AGE_MS = 8_000;

/** The current time, updated every second. */
function useNow(): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, []);
  return now;
}

/** The last live rate pushed on /ws, while it is fresh. */
function useLiveRate(now: number): LiveRate | undefined {
  const live = useQuery<LiveRate>({
    queryKey: LIVE_RATE_KEY,
    queryFn: () => Promise.reject(new Error("pushed only")),
    enabled: false,
    refetchInterval: false,
  });
  const fresh = live.data && now - live.data.receivedAt < LIVE_MAX_AGE_MS;
  return fresh ? live.data : undefined;
}

/** The chart's window: the last 6 hours. */
const CHART_WINDOW_SECONDS = 6 * 3600;

/**
 * The chosen ports' devices without fresh data: a polled device whose last successful poll
 * is older than 2 x its interval + 30 s, or a scraped one Prometheus can't scrape.
 */
function useStaleDevices(ports: PortRef[], instances: Instance[], nowMs: number): Instance[] {
  const ids = new Set(ports.map((p) => p.instanceId));
  const chosen = instances.filter((i) => ids.has(i.id));
  const scrape = useScrapeStates(chosen);
  return chosen.filter((i) => {
    const source = sourceOf(i);
    if (source.kind === "polled") {
      return (
        source.lastSuccess !== null &&
        nowMs / 1000 - source.lastSuccess > freshFor(POLL_INTERVAL_SECONDS)
      );
    }
    return source.kind === "scraped" && shownState(i, scrape) === "down";
  });
}

function Current({ label, value, tone }: { label: string; value: string; tone: "in" | "out" }) {
  return (
    <div role="group" aria-label={label} className="flex flex-col">
      <span className="flex items-center gap-1.5 text-xs text-muted">
        <span className={`h-[3px] w-2.5 rounded-sm ${tone === "in" ? "bg-in" : "bg-out"}`} />
        {label}
      </span>
      <span className="text-xl font-medium tabular-nums">{value}</span>
    </div>
  );
}

/**
 * In and out traffic of the ports the admin chose (summed): now, and the last 6 hours. While
 * the backend pushes live rates (every ~2 s), those replace Prometheus' 2-minute average.
 */
export function ThroughputCard({ instances, settings }: WidgetProps) {
  const { formatRate, unit } = useUnits();
  const ports = portsFromSettings(settings);
  const chosen = ports.length > 0;
  const inExpr = rateQuery(ports, "in");
  const outExpr = rateQuery(ports, "out");
  const rx = useFleetQuery(inExpr);
  const tx = useFleetQuery(outExpr);
  const rxHistory = useFleetRangeQuery(inExpr, CHART_WINDOW_SECONDS, chosen);
  const txHistory = useFleetRangeQuery(outExpr, CHART_WINDOW_SECONDS, chosen);
  const { groups } = usePortOptions(instances);
  const now = useNow();
  // Live only when every chosen port was read; a partial sum would look like the total.
  const pushed = useLiveRate(now);
  const live = pushed && pushed.ports === ports.length ? pushed : undefined;
  const staleDevices = useStaleDevices(ports, instances, now);

  if (!chosen) {
    return (
      <div className="flex h-full flex-col justify-center gap-1 p-4">
        <span className="text-xs text-slate-500 dark:text-slate-400">Throughput</span>
        <span className="text-sm">No port chosen.</span>
        <span className="text-xs text-slate-500 dark:text-slate-400">
          Edit dashboard, then ⚙ on this card.
        </span>
      </div>
    );
  }

  const names = new Map(
    groups.flatMap((g) => g.ports.map((o) => [portKey(o.port), o.name] as const)),
  );
  const label = ports.map((p) => names.get(portKey(p)) ?? p.iface).join(", ");
  // In/out as the port counts it. Only for WAN ports is that the network's download/upload.
  const wan = ports.every((p) => isWanPort(p.iface, names.get(portKey(p))));
  const [inName, outName] = wan ? ["Download", "Upload"] : ["In", "Out"];
  // A missing counter sums as 0 ("or vector(0)"), so with a device not answering "now" would
  // read 0: it shows a dash instead, and the history is greyed.
  const stale = !live && staleDevices.length > 0;
  const current = (pushedBps: number | undefined, q: typeof rx) =>
    pushedBps !== undefined
      ? formatRate(pushedBps)
      : stale || q.isError
        ? "—"
        : formatRate(firstValue(q.data));
  const historyError = rxHistory.error ?? txHistory.error;
  const chart = (
    <AreaChart
      label={`${inName} and ${outName.toLowerCase()} over the last 6 hours`}
      format={formatRate}
      tickScale={unit === "bytes" ? 8 : 1}
      series={[
        { name: inName, points: firstSeries(rxHistory.data), tone: "in", area: true },
        { name: outName, points: firstSeries(txHistory.data), tone: "out" },
      ]}
    />
  );
  let history;
  if (historyError) {
    history = (
      <p role="alert" className="line-clamp-2 text-xs text-crit-text" title={historyError.message}>
        Could not load the history: {historyError.message}
      </p>
    );
  } else if (rxHistory.isPending || txHistory.isPending) {
    history = <p className="text-xs text-muted">Loading…</p>;
  } else if (stale) {
    history = (
      <div data-stale="true" className="flex min-h-0 flex-1 flex-col opacity-60 grayscale">
        <StaleContext.Provider value>{chart}</StaleContext.Provider>
      </div>
    );
  } else {
    history = chart;
  }
  return (
    <section aria-label="Throughput" className="flex h-full min-w-0 flex-col gap-3 p-4">
      <div className="flex flex-wrap items-start gap-x-4 gap-y-2">
        <div className="min-w-0">
          <div className="text-[15px] font-semibold">Throughput</div>
          <div className="truncate text-xs text-muted" title={label}>
            {label}
            {live ? " · live" : ""}
          </div>
        </div>
        <div className="ml-auto flex gap-5">
          <Current label={inName} tone="in" value={current(live?.inBps, rx)} />
          <Current label={outName} tone="out" value={current(live?.outBps, tx)} />
        </div>
      </div>
      <div className="flex min-h-40 flex-1 flex-col">{history}</div>
      {stale && (
        <p className="text-xs text-muted">
          No recent data from {staleDevices.map((i) => i.name).join(", ")}; history greyed.
        </p>
      )}
    </section>
  );
}

/** The port picker, shown in edit mode. */
export function ThroughputSettings({ instances, settings, onChange }: WidgetSettingsProps) {
  const { groups, loading } = usePortOptions(instances);
  const chosen = portsFromSettings(settings);
  const chosenKeys = new Set(chosen.map(portKey));
  const full = chosen.length >= MAX_PORTS;

  if (loading) return <p className="text-sm text-slate-600 dark:text-slate-400">Loading ports…</p>;
  if (groups.length === 0)
    return (
      <p className="text-sm text-slate-600 dark:text-slate-400">
        No device reports traffic counters yet.
      </p>
    );
  return (
    <div className="flex flex-col gap-3">
      <p className="text-sm text-slate-600 dark:text-slate-400">
        The card adds up the traffic of the ports you tick (up to {MAX_PORTS}). For internet
        traffic, tick your firewall's WAN port.
      </p>
      {groups.map(({ instance, ports }) => (
        <fieldset key={instance.id} className="flex flex-col gap-1">
          <legend className="text-xs tracking-wide text-slate-500 uppercase dark:text-slate-400">
            {instance.name}
          </legend>
          <div className="flex flex-wrap gap-x-4 gap-y-1">
            {ports.map((o) => {
              const key = portKey(o.port);
              const checked = chosenKeys.has(key);
              return (
                <label key={key} className="flex items-center gap-1.5 text-sm">
                  <input
                    type="checkbox"
                    checked={checked}
                    disabled={!checked && full}
                    onChange={() =>
                      onChange(
                        settingsWithPorts(
                          settings,
                          checked ? chosen.filter((p) => portKey(p) !== key) : [...chosen, o.port],
                        ),
                      )
                    }
                  />
                  {o.name}
                  {o.detail && (
                    <span className="text-xs text-slate-500 dark:text-slate-400">({o.detail})</span>
                  )}
                </label>
              );
            })}
          </div>
        </fieldset>
      ))}
    </div>
  );
}
