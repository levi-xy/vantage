import { useState } from "react";
import type { QueryResult } from "../../api";
import type { CardProps } from "../registry";
import { CardFrame } from "../metrics/CardFrame";
import { formatBitsPerSecond } from "../metrics/format";
import { parseValue } from "../metrics/promql";
import { useInstantQuery } from "../metrics/useMetricQuery";
import { IF_DOWN, IF_UP, snmpMetric } from "../snmp-overview/snmpMetrics";

type PortState = "up" | "down" | "disabled";

interface Port {
  index: string;
  name: string;
  alias: string;
  state: PortState;
  speed?: number;
  inBps?: number;
  outBps?: number;
}

const TILE: Record<PortState, string> = {
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

function speedText(bps: number | undefined): string {
  return bps ? formatBitsPerSecond(bps) : "–";
}

function describe(p: Port): string {
  const name = p.alias ? `${p.name} (${p.alias})` : p.name;
  return `${name}: ${p.state}, ${speedText(p.speed)}, in ${formatBitsPerSecond(p.inBps)}, out ${formatBitsPerSecond(p.outBps)}`;
}

export function SnmpPortsCard({ instanceId, title, instance }: CardProps) {
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
  const [selected, setSelected] = useState<string | null>(null);

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

  const talkers = ports
    .map((p) => ({ port: p, total: (p.inBps ?? 0) + (p.outBps ?? 0) }))
    .filter((t) => t.total > 0)
    .sort((a, b) => b.total - a.total)
    .slice(0, 3);
  const chosen = ports.find((p) => p.index === selected);

  return (
    <CardFrame
      title={title}
      instance={instance}
      loading={oper.isPending}
      error={oper.error}
      empty={ports.length === 0}
    >
      <div
        role="group"
        aria-label="Ports"
        className="grid grid-cols-[repeat(auto-fill,minmax(3.5rem,1fr))] gap-1.5"
      >
        {ports.map((p) => (
          <button
            key={p.index}
            type="button"
            aria-pressed={selected === p.index}
            aria-label={`${p.name}: ${p.state}`}
            title={describe(p)}
            onClick={() => setSelected(selected === p.index ? null : p.index)}
            className={`flex flex-col items-center rounded border px-1 py-1 text-xs leading-tight ${TILE[p.state]} ${selected === p.index ? "ring-2 ring-blue-500" : ""}`}
          >
            <span className="w-full truncate text-center font-medium">{p.name}</span>
            <span className="tabular-nums opacity-80">
              {p.state === "up" ? speedText(p.speed) : p.state}
            </span>
          </button>
        ))}
      </div>

      <p className="min-h-5 text-xs text-slate-600 dark:text-slate-300" aria-live="polite">
        {chosen ? describe(chosen) : "Tap a port for details."}
      </p>

      {talkers.length > 0 && (
        <section aria-label="Top talkers" className="flex flex-col gap-0.5 text-xs">
          <span className="text-slate-500 dark:text-slate-400">Top talkers</span>
          {talkers.map(({ port, total }) => (
            <div key={port.index} className="flex justify-between gap-2">
              <span className="truncate">{port.name}</span>
              <span className="tabular-nums">{formatBitsPerSecond(total)}</span>
            </div>
          ))}
        </section>
      )}
    </CardFrame>
  );
}
