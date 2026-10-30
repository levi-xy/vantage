import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { LIVE_RATE_KEY } from "../../app/live";
import type { LiveRate } from "../../app/live";
import { useUnits } from "../../app/units";
import { firstSeries, firstValue } from "../metrics/promql";
import { StatTile } from "../metrics/StatTile";
import { useFleetQuery, useFleetRangeQuery } from "../metrics/useMetricQuery";
import type { WidgetProps, WidgetSettingsProps } from "../widgets";
import {
  MAX_PORTS,
  PORT_LIST_QUERIES,
  portKey,
  portOptions,
  portsFromSettings,
  rateQuery,
  settingsWithPorts,
  SNMP_INFO_QUERY,
} from "./ports";
import type { Instance } from "../../api";

/** Every port Vantage has traffic counters for, named as the cards name them. */
function usePortOptions(instances: Instance[]) {
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
function useLiveRate(): LiveRate | undefined {
  const now = useNow();
  const live = useQuery<LiveRate>({
    queryKey: LIVE_RATE_KEY,
    queryFn: () => Promise.reject(new Error("pushed only")),
    enabled: false,
    refetchInterval: false,
  });
  const fresh = live.data && now - live.data.receivedAt < LIVE_MAX_AGE_MS;
  return fresh ? live.data : undefined;
}

/**
 * In and out traffic of the ports the admin chose (summed), with the last hour. While the
 * backend pushes live rates (every ~2 s), those replace Prometheus' 2-minute average.
 */
export function ThroughputCard({ instances, settings }: WidgetProps) {
  const { formatRate } = useUnits();
  const ports = portsFromSettings(settings);
  const chosen = ports.length > 0;
  const inExpr = rateQuery(ports, "in");
  const outExpr = rateQuery(ports, "out");
  const rx = useFleetQuery(inExpr);
  const tx = useFleetQuery(outExpr);
  const rxSpark = useFleetRangeQuery(inExpr, undefined, chosen);
  const txSpark = useFleetRangeQuery(outExpr, undefined, chosen);
  const { groups } = usePortOptions(instances);
  // Live only when every chosen port was read; a partial sum would look like the total.
  const pushed = useLiveRate();
  const live = pushed && pushed.ports === ports.length ? pushed : undefined;

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
  return (
    <div className="flex h-full min-w-0 flex-col gap-0.5 px-4 py-3">
      <span className="truncate text-xs text-slate-500 dark:text-slate-400" title={label}>
        {live && (
          <span
            className="mr-1 inline-block h-1.5 w-1.5 rounded-full bg-emerald-500 align-middle"
            aria-label="live"
          />
        )}
        Throughput · {label}
      </span>
      <div className="grid grid-cols-2 gap-3">
        <StatTile
          label="↓ In"
          value={live ? formatRate(live.inBps) : rx.isError ? "—" : formatRate(firstValue(rx.data))}
          spark={firstSeries(rxSpark.data)}
        />
        <StatTile
          label="↑ Out"
          value={
            live ? formatRate(live.outBps) : tx.isError ? "—" : formatRate(firstValue(tx.data))
          }
          spark={firstSeries(txSpark.data)}
        />
      </div>
    </div>
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
