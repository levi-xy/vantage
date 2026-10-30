// Port data shared by the SNMP cards (Ports, Switch panel): one row per interface.
import type { QueryResult } from "../../api";
import { formatBitsPerSecond } from "../metrics/format";
import { parseValue } from "../metrics/promql";
import { useInstantQuery } from "../metrics/useMetricQuery";
import { IF_DOWN, IF_UP, snmpMetric } from "../snmp-overview/snmpMetrics";

export type PortState = "up" | "down" | "disabled";

export interface Port {
  index: string;
  /** The stable port id from the metric labels (if_name: port3, sfpplus9, eth0). */
  id: string;
  /** What to show: the name set on the device when there is one, else the id. */
  name: string;
  alias: string;
  state: PortState;
  speed?: number;
  inBps?: number;
  outBps?: number;
}

export const TILE: Record<PortState, string> = {
  up: "border-emerald-600 bg-emerald-100 text-emerald-900 dark:border-emerald-500 dark:bg-emerald-950 dark:text-emerald-200",
  down: "border-red-600 bg-red-100 text-red-900 dark:border-red-500 dark:bg-red-950 dark:text-red-200",
  disabled:
    "border-slate-400 bg-slate-100 text-slate-600 dark:border-slate-600 dark:bg-slate-800 dark:text-slate-400",
};

/** Value per `if_index`. */
function byIndex(result: QueryResult | undefined): Map<string, number | undefined> {
  const out = new Map<string, number | undefined>();
  for (const s of result?.series ?? []) {
    out.set(s.labels.if_index ?? "", parseValue(s.values[0]?.[1]));
  }
  return out;
}

export function speedText(bps: number | undefined): string {
  return bps ? formatBitsPerSecond(bps) : "–";
}

export function describe(p: Port, formatRate: (bps: number | undefined) => string): string {
  const name = p.alias ? `${p.name} (${p.alias})` : p.name;
  return `${name}: ${p.state}, ${speedText(p.speed)}, in ${formatRate(p.inBps)}, out ${formatRate(p.outBps)}`;
}

/** Every port of an SNMP instance, sorted by ifIndex, with state, speed and rates. */
export function useSnmpPorts(instanceId: string) {
  const oper = useInstantQuery(instanceId, (sel) => snmpMetric(sel, "if_oper_status"));
  const admin = useInstantQuery(instanceId, (sel) => snmpMetric(sel, "if_admin_status"));
  const speed = useInstantQuery(instanceId, (sel) => snmpMetric(sel, "if_speed_bits_per_second"));
  const rx = useInstantQuery(
    instanceId,
    (sel) => `8 * rate(${snmpMetric(sel, "if_in_bytes_total")}[5m])`,
  );
  const tx = useInstantQuery(
    instanceId,
    (sel) => `8 * rate(${snmpMetric(sel, "if_out_bytes_total")}[5m])`,
  );
  // The interface alias is free text, so it is only on the info series: join on if_index.
  const info = useInstantQuery(instanceId, (sel) => snmpMetric(sel, "if_info"));

  const adminBy = byIndex(admin.data);
  const speedBy = byIndex(speed.data);
  const rxBy = byIndex(rx.data);
  const txBy = byIndex(tx.data);
  const infoBy = new Map(
    (info.data?.series ?? []).map((s) => [s.labels.if_index ?? "", s.labels] as const),
  );

  const ports: Port[] = (oper.data?.series ?? [])
    .map((s): Port => {
      const index = s.labels.if_index ?? "";
      const operValue = parseValue(s.values[0]?.[1]);
      const state: PortState =
        adminBy.get(index) === IF_DOWN ? "disabled" : operValue === IF_UP ? "up" : "down";
      const labels = infoBy.get(index);
      return {
        index,
        id: s.labels.if_name ?? labels?.if_name ?? "",
        // A MikroTik port's own name (if_label) is display text on the info series only.
        name: labels?.if_label || s.labels.if_name || labels?.if_name || `if${index}`,
        alias: labels?.if_alias ?? "",
        state,
        speed: speedBy.get(index),
        inBps: rxBy.get(index),
        outBps: txBy.get(index),
      };
    })
    .sort((a, b) => Number(a.index) - Number(b.index));
  return { ports, loading: oper.isPending, error: oper.error };
}
