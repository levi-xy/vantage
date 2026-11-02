import type { CardProps } from "../registry";
import { cardFreshness } from "../metrics/freshness";
import { CardFrame } from "../metrics/CardFrame";
import { byLabel, firstValue } from "../metrics/promql";
import { StatTile } from "../metrics/StatTile";
import { useInstantQuery } from "../metrics/useMetricQuery";
import { isInternal } from "../snmp-ports/ports";
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
  const known = byLabel(oper.data, "if_index").filter((p) => p.value !== undefined);
  // Physical ports only: loopbacks and tunnels are not counted (see `isInternal`).
  const ports = known.filter((p) => !isInternal({ id: p.labels.if_name ?? "", name: "" }));
  const internal = known.length - ports.length;
  const up = ports.filter((p) => p.value === IF_UP).length;
  const down = ports.length - up;

  return (
    <CardFrame
      title={title}
      showTitle={false}
      instance={instance}
      loading={info.isPending && uptime.isPending}
      error={info.error ?? uptime.error}
      freshness={cardFreshness(info, uptime, oper)}
      empty={!labels && uptimeValue === undefined}
      uptimeSeconds={uptimeValue}
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
      <div className="grid grid-cols-2 gap-3">
        <StatTile label="Interfaces up" value={ports.length ? String(up) : "–"} />
        <StatTile
          label="Interfaces down"
          value={ports.length ? String(down) : "–"}
          tone={down > 0 ? "bad" : undefined}
        />
      </div>
      {internal > 0 && (
        <p className="text-xs text-muted">
          Physical ports only; {internal} internal (loopback, tunnel) not counted.
        </p>
      )}
    </CardFrame>
  );
}
