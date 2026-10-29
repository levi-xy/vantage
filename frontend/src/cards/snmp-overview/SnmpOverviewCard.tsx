import type { CardProps } from "../registry";
import { CardFrame } from "../metrics/CardFrame";
import { formatDuration } from "../metrics/format";
import { byLabel, firstValue } from "../metrics/promql";
import { StatTile } from "../metrics/StatTile";
import { useInstantQuery } from "../metrics/useMetricQuery";
import { IF_UP, snmpMetric } from "./snmpMetrics";

export function SnmpOverviewCard({ instanceId, title, instance }: CardProps) {
  // Free text (name, description) is only on the `*_info` series.
  const info = useInstantQuery(instanceId, (sel) => snmpMetric(sel, "info"));
  const uptime = useInstantQuery(
    instanceId,
    (sel) => `sum without(source) (${snmpMetric(sel, "uptime_seconds")})`,
  );
  const oper = useInstantQuery(instanceId, (sel) => snmpMetric(sel, "if_oper_status"));

  const labels = info.data?.series[0]?.labels;
  const uptimeValue = firstValue(uptime.data);
  const ports = byLabel(oper.data, "if_index").filter((p) => p.value !== undefined);
  const up = ports.filter((p) => p.value === IF_UP).length;
  const down = ports.length - up;

  return (
    <CardFrame
      title={title}
      instance={instance}
      loading={info.isPending && uptime.isPending}
      error={info.error ?? uptime.error}
      empty={!labels && uptimeValue === undefined}
    >
      <div>
        <div className="truncate text-base font-semibold">
          {labels?.sys_name || "Unnamed device"}
        </div>
        {labels?.sys_descr && (
          <div className="line-clamp-2 text-xs text-slate-500 dark:text-slate-400">
            {labels.sys_descr}
          </div>
        )}
      </div>
      <div className="grid grid-cols-3 gap-3">
        <StatTile label="Uptime" value={formatDuration(uptimeValue)} />
        <StatTile label="Interfaces up" value={ports.length ? String(up) : "–"} />
        <StatTile
          label="Interfaces down"
          value={ports.length ? String(down) : "–"}
          tone={down > 0 ? "bad" : undefined}
        />
      </div>
    </CardFrame>
  );
}
