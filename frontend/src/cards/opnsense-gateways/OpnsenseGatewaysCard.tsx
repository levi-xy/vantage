import type { CardProps } from "../registry";
import { cardFreshness } from "../metrics/freshness";
import { CardFrame } from "../metrics/CardFrame";
import { formatMs } from "../metrics/format";
import { byLabel } from "../metrics/promql";
import { Figures, KeyFigure } from "../metrics/KeyFigure";
import { StatTile } from "../metrics/StatTile";
import { useCardTier } from "../metrics/tier";
import { useInstantQuery } from "../metrics/useMetricQuery";

const P = "vantage_opnsense_gateway_";
type Sel = (extra?: string) => string;

/** Gateways: up or down, latency and packet loss. */
export function OpnsenseGatewaysCard({ instanceId, title, instance }: CardProps) {
  const tier = useCardTier();
  const up = useInstantQuery(instanceId, (sel: Sel) => `${P}up${sel()}`);
  const delay = useInstantQuery(instanceId, (sel: Sel) => `${P}delay_seconds${sel()}`);
  const loss = useInstantQuery(instanceId, (sel: Sel) => `${P}loss_percent${sel()}`);

  const delayBy = new Map(byLabel(delay.data, "gateway").map((x) => [x.key, x.value] as const));
  const lossBy = new Map(byLabel(loss.data, "gateway").map((x) => [x.key, x.value] as const));
  const rows = byLabel(up.data, "gateway")
    .map((r) => ({ name: r.key, up: r.value === 1 }))
    .sort((a, b) => Number(a.up) - Number(b.up) || a.name.localeCompare(b.name));
  const down = rows.filter((r) => !r.up).length;

  return (
    <CardFrame
      title={title}
      instance={instance}
      loading={up.isPending}
      error={up.error}
      freshness={cardFreshness(up, delay, loss)}
      empty={rows.length === 0}
      emptyHint="No gateways reported yet."
      problem={down > 0 ? `${down} DOWN` : undefined}
    >
      {tier === "small" ? (
        <KeyFigure label="Gateways" tone={down > 0 ? "bad" : undefined}>
          {down > 0 ? `${down} gateway${down === 1 ? "" : "s"} down` : `${rows.length} up`}
        </KeyFigure>
      ) : tier === "medium" ? (
        <Figures>
          <StatTile label="Up" value={String(rows.length - down)} tone="ok" />
          <StatTile label="Down" value={String(down)} tone={down > 0 ? "bad" : undefined} />
        </Figures>
      ) : (
        <ul
          aria-label="Gateways"
          className="flex flex-col divide-y divide-slate-200 dark:divide-slate-800"
        >
          {rows.map((r) => {
            const lossValue = lossBy.get(r.name);
            return (
              <li key={r.name} className="flex items-center gap-2 py-1.5 text-sm">
                <span
                  aria-hidden="true"
                  className={`h-2 w-2 shrink-0 rounded-full ${r.up ? "bg-ok-text" : "bg-crit-text"}`}
                />
                <span className="min-w-0 flex-1 truncate font-medium">{r.name}</span>
                <span className="shrink-0 text-right text-xs tabular-nums">
                  {r.up ? "up" : "down"} · {formatMs(delayBy.get(r.name))} · loss{" "}
                  {lossValue === undefined ? "–" : `${lossValue.toFixed(lossValue >= 10 ? 0 : 1)}%`}
                </span>
              </li>
            );
          })}
        </ul>
      )}
    </CardFrame>
  );
}
