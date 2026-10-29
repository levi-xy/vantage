import type { CardProps } from "../registry";
import { CardFrame } from "../metrics/CardFrame";
import { formatBitsPerSecond, formatPercent } from "../metrics/format";
import { byLabel, firstSeries, firstValue } from "../metrics/promql";
import { StatTile } from "../metrics/StatTile";
import { useInstantQuery, useRangeQuery } from "../metrics/useMetricQuery";

// Scraped from Telegraf on pfSense (native names). Skip loopback and pf's pseudo interfaces.
const REAL_IF = 'interface!~"lo.*|pflog.*|pfsync.*|enc.*"';
type Sel = (extra?: string) => string;
const cpuExpr = (sel: Sel) => `100 - cpu_usage_idle${sel('cpu="cpu-total"')}`;
const memExpr = (sel: Sel) => `mem_used_percent${sel()}`;
const statesExpr = (sel: Sel) => `pf_entries${sel()}`;

export function PfsenseOverviewCard({ instanceId, title, instance }: CardProps) {
  const cpu = useInstantQuery(instanceId, cpuExpr);
  const mem = useInstantQuery(instanceId, memExpr);
  const states = useInstantQuery(instanceId, statesExpr);
  const rx = useInstantQuery(instanceId, (sel) => `8 * rate(net_bytes_recv${sel(REAL_IF)}[5m])`);
  const tx = useInstantQuery(instanceId, (sel) => `8 * rate(net_bytes_sent${sel(REAL_IF)}[5m])`);
  const cpuSpark = useRangeQuery(instanceId, cpuExpr);
  const memSpark = useRangeQuery(instanceId, memExpr);
  const statesSpark = useRangeQuery(instanceId, statesExpr);

  const cpuValue = firstValue(cpu.data);
  const memValue = firstValue(mem.data);
  const statesValue = firstValue(states.data);

  const txBy = new Map(byLabel(tx.data, "interface").map((r) => [r.key, r.value] as const));
  const interfaces = byLabel(rx.data, "interface")
    .map((r) => ({ name: r.key, rx: r.value, tx: txBy.get(r.key) }))
    .sort((a, b) => a.name.localeCompare(b.name));

  return (
    <CardFrame
      title={title}
      instance={instance}
      loading={cpu.isPending && mem.isPending}
      error={cpu.error ?? mem.error}
      empty={cpuValue === undefined && memValue === undefined && statesValue === undefined}
    >
      <div className="grid grid-cols-3 gap-3">
        <StatTile label="CPU" value={formatPercent(cpuValue)} spark={firstSeries(cpuSpark.data)} />
        <StatTile
          label="Memory"
          value={formatPercent(memValue)}
          spark={firstSeries(memSpark.data)}
        />
        <StatTile
          label="States"
          value={statesValue === undefined ? "–" : String(Math.round(statesValue))}
          spark={firstSeries(statesSpark.data)}
        />
      </div>
      {interfaces.length > 0 && (
        <section aria-label="Interfaces" className="flex flex-col gap-1 text-sm">
          {interfaces.map((i) => (
            <div key={i.name} className="flex flex-wrap justify-between gap-x-3">
              <span className="font-medium">{i.name}</span>
              <span className="tabular-nums text-slate-600 dark:text-slate-300">
                ↓ {formatBitsPerSecond(i.rx)} · ↑ {formatBitsPerSecond(i.tx)}
              </span>
            </div>
          ))}
        </section>
      )}
    </CardFrame>
  );
}
