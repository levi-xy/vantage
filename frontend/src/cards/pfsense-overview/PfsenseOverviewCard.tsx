import type { CardProps } from "../registry";
import { CardFrame } from "../metrics/CardFrame";
import { formatCelsius, formatDuration, formatPercent } from "../metrics/format";
import { firstSeries, firstValue } from "../metrics/promql";
import { StatTile } from "../metrics/StatTile";
import { useInstantQuery, useRangeQuery } from "../metrics/useMetricQuery";

// Scraped from Telegraf on pfSense (native names). The card type stays "pfsense-overview"
// so dashboards saved before it became the Resources card keep it.
type Sel = (extra?: string) => string;
const cpuExpr = (sel: Sel) => `100 - cpu_usage_idle${sel('cpu="cpu-total"')}`;
const memExpr = (sel: Sel) => `mem_used_percent${sel()}`;
const diskExpr = (sel: Sel) => `disk_used_percent${sel('path="/"')}`;
const statesExpr = (sel: Sel) => `pf_entries${sel()}`;
// Telegraf's `temp` input, only where the hardware reports a sensor: the hottest one.
const tempExpr = (sel: Sel) => `max(temp_temp${sel()})`;
const uptimeExpr = (sel: Sel) => `system_uptime${sel()}`;

/** CPU, memory, storage, firewall states, uptime, and temperature when there is a sensor. */
export function PfsenseOverviewCard({ instanceId, title, instance }: CardProps) {
  const cpu = useInstantQuery(instanceId, cpuExpr);
  const mem = useInstantQuery(instanceId, memExpr);
  const disk = useInstantQuery(instanceId, diskExpr);
  const states = useInstantQuery(instanceId, statesExpr);
  const temp = useInstantQuery(instanceId, tempExpr);
  const uptime = useInstantQuery(instanceId, uptimeExpr);
  const cpuSpark = useRangeQuery(instanceId, cpuExpr);
  const memSpark = useRangeQuery(instanceId, memExpr);

  const cpuValue = firstValue(cpu.data);
  const memValue = firstValue(mem.data);
  const statesValue = firstValue(states.data);
  const tempValue = firstValue(temp.data);

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
        <StatTile label="Storage" value={formatPercent(firstValue(disk.data))} />
        <StatTile
          label="States"
          value={statesValue === undefined ? "–" : String(Math.round(statesValue))}
        />
        <StatTile label="Uptime" value={formatDuration(firstValue(uptime.data))} />
        {tempValue !== undefined && (
          <StatTile label="Temperature" value={formatCelsius(tempValue)} />
        )}
      </div>
    </CardFrame>
  );
}
