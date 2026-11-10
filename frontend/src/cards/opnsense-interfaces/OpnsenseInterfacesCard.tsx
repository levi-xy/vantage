import type { CardProps } from "../registry";
import { cardFreshness } from "../metrics/freshness";
import { CardFrame } from "../metrics/CardFrame";
import { formatBitsPerSecond } from "../metrics/format";
import { byLabel } from "../metrics/promql";
import { Figures, KeyFigure } from "../metrics/KeyFigure";
import { StatTile } from "../metrics/StatTile";
import { useCardTier } from "../metrics/tier";
import { useInstantQuery } from "../metrics/useMetricQuery";

const P = "vantage_opnsense_interface_";
type Sel = (extra?: string) => string;

/** Interfaces with link state and in/out rates. Names come from the `interface_info` series. */
export function OpnsenseInterfacesCard({ instanceId, title, instance }: CardProps) {
  const tier = useCardTier();
  const up = useInstantQuery(instanceId, (sel: Sel) => `${P}up${sel()}`);
  const rx = useInstantQuery(
    instanceId,
    (sel: Sel) => `8 * rate(${P}receive_bytes_total${sel()}[5m])`,
  );
  const tx = useInstantQuery(
    instanceId,
    (sel: Sel) => `8 * rate(${P}transmit_bytes_total${sel()}[5m])`,
  );
  const info = useInstantQuery(instanceId, (sel: Sel) => `${P}info${sel()}`);

  const rxBy = new Map(byLabel(rx.data, "interface").map((x) => [x.key, x.value] as const));
  const txBy = new Map(byLabel(tx.data, "interface").map((x) => [x.key, x.value] as const));
  const nameBy = new Map(
    byLabel(info.data, "interface").map((x) => [x.key, x.labels.interface_name ?? ""] as const),
  );
  const rows = byLabel(up.data, "interface")
    .map((r) => ({ device: r.key, up: r.value === 1, name: nameBy.get(r.key) || r.key }))
    .sort((a, b) => Number(b.up) - Number(a.up) || a.name.localeCompare(b.name));
  const upCount = rows.filter((r) => r.up).length;

  return (
    <CardFrame
      title={title}
      instance={instance}
      loading={up.isPending}
      error={up.error}
      freshness={cardFreshness(up, rx, tx, info)}
      empty={rows.length === 0}
      emptyHint="No interfaces reported yet."
    >
      {tier === "small" ? (
        <KeyFigure label="Interfaces up">
          {upCount}/{rows.length} up
        </KeyFigure>
      ) : tier === "medium" ? (
        <Figures>
          <StatTile label="Up" value={String(upCount)} />
          <StatTile label="Down" value={String(rows.length - upCount)} />
        </Figures>
      ) : (
        <ul
          aria-label="Interfaces"
          className="flex flex-col divide-y divide-slate-200 dark:divide-slate-800"
        >
          {rows.map((r) => (
            <li key={r.device} className="flex items-center gap-2 py-1.5 text-sm">
              <span
                aria-hidden="true"
                className={`h-2 w-2 shrink-0 rounded-full ${r.up ? "bg-ok-text" : "bg-crit-text"}`}
              />
              <span className="min-w-0 flex-1">
                <span className="block truncate font-medium">{r.name}</span>
                {r.name !== r.device && (
                  <span className="block text-xs text-slate-500 dark:text-slate-400">
                    {r.device}
                  </span>
                )}
              </span>
              <span className="shrink-0 text-right text-xs tabular-nums">
                <span className="block">in {formatBitsPerSecond(rxBy.get(r.device))}</span>
                <span className="block">out {formatBitsPerSecond(txBy.get(r.device))}</span>
              </span>
              <span className="sr-only">{r.up ? "up" : "down"}</span>
            </li>
          ))}
        </ul>
      )}
    </CardFrame>
  );
}
