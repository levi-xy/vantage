import type { CardProps } from "../registry";
import { cardFreshness } from "../metrics/freshness";
import { CardFrame } from "../metrics/CardFrame";
import { byLabel } from "../metrics/promql";
import { Figures, KeyFigure } from "../metrics/KeyFigure";
import { StatTile } from "../metrics/StatTile";
import { useCardTier } from "../metrics/tier";
import { useInstantQuery } from "../metrics/useMetricQuery";

type Sel = (extra?: string) => string;

/** Services: stopped ones first, so a problem is at the top. */
export function OpnsenseServicesCard({ instanceId, title, instance }: CardProps) {
  const tier = useCardTier();
  const running = useInstantQuery(
    instanceId,
    (sel: Sel) => `vantage_opnsense_service_running${sel()}`,
  );
  const rows = byLabel(running.data, "service")
    .map((r) => ({ name: r.key, running: r.value === 1 }))
    .sort((a, b) => Number(a.running) - Number(b.running) || a.name.localeCompare(b.name));
  const runningCount = rows.filter((r) => r.running).length;

  return (
    <CardFrame
      title={title}
      instance={instance}
      loading={running.isPending}
      error={running.error}
      freshness={cardFreshness(running)}
      empty={rows.length === 0}
      emptyHint="No services reported yet."
    >
      {tier === "small" ? (
        <KeyFigure label="Services running">
          {runningCount}/{rows.length} running
        </KeyFigure>
      ) : tier === "medium" ? (
        <Figures>
          <StatTile label="Running" value={String(runningCount)} />
          <StatTile label="Stopped" value={String(rows.length - runningCount)} />
        </Figures>
      ) : (
        <ul
          aria-label="Services"
          className="flex flex-col divide-y divide-slate-200 dark:divide-slate-800"
        >
          {rows.map((r) => (
            <li key={r.name} className="flex items-center gap-2 py-1.5 text-sm">
              <span className="min-w-0 flex-1 truncate font-medium">{r.name}</span>
              <span className={`shrink-0 text-xs ${r.running ? "text-muted" : "text-crit-text"}`}>
                {r.running ? "running" : "stopped"}
              </span>
            </li>
          ))}
        </ul>
      )}
    </CardFrame>
  );
}
