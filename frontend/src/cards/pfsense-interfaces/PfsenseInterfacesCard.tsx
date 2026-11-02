import { useState } from "react";
import type { CardProps } from "../registry";
import { cardFreshness } from "../metrics/freshness";
import { CardFrame } from "../metrics/CardFrame";
import { byLabel } from "../metrics/promql";
import { useInstantQuery } from "../metrics/useMetricQuery";
import { interfaceSettings, REAL_IF } from "./interfaceSettings";
import { interfaceRows } from "./interfaceRows";
import { useUnits } from "../../app/units";

/** Rows shown before "Show all". */
export const COLLAPSED_ROWS = 6;

/**
 * Traffic per interface, under the names set in the pfSense device settings: WAN pinned,
 * busiest first, VLANs under their parent.
 */
export function PfsenseInterfacesCard({ instanceId, title, instance }: CardProps) {
  const { formatRate } = useUnits();
  const [expanded, setExpanded] = useState(false);
  const rx = useInstantQuery(instanceId, (sel) => `8 * rate(net_bytes_recv${sel(REAL_IF)}[5m])`);
  const tx = useInstantQuery(instanceId, (sel) => `8 * rate(net_bytes_sent${sel(REAL_IF)}[5m])`);
  const settings = interfaceSettings(instance);

  const txBy = new Map(byLabel(tx.data, "interface").map((r) => [r.key, r.value] as const));
  const rows = interfaceRows(
    byLabel(rx.data, "interface")
      .filter((r) => !settings.hidden(r.key))
      .map((r) => ({ id: r.key, name: settings.label(r.key), rx: r.value, tx: txBy.get(r.key) })),
  );
  const busiest = Math.max(0, ...rows.map((r) => r.total));
  const shown = expanded ? rows : rows.slice(0, COLLAPSED_ROWS);

  return (
    <CardFrame
      title={title}
      instance={instance}
      loading={rx.isPending}
      error={rx.error ?? tx.error}
      freshness={cardFreshness(rx, tx)}
      empty={rows.length === 0}
    >
      <ul aria-label="Interfaces" className="flex flex-col gap-2 text-sm">
        {shown.map((r) => (
          <li
            key={r.id}
            data-child={r.child || undefined}
            className={`grid grid-cols-[minmax(0,1fr)_3rem_auto] items-center gap-2.5 ${r.child ? "pl-3.5" : ""}`}
          >
            <span className="flex min-w-0 items-baseline gap-1.5 overflow-hidden whitespace-nowrap">
              {r.child && (
                <span aria-hidden="true" className="text-xs text-muted">
                  └
                </span>
              )}
              <span className={`truncate ${r.child ? "" : "font-medium"}`}>{r.name}</span>
              {r.name !== r.id && <span className="truncate text-[11px] text-muted">{r.id}</span>}
              {r.pinned && <span className="sr-only">(pinned)</span>}
            </span>
            <span aria-hidden="true" className="h-[5px] overflow-hidden rounded-full bg-track">
              <span
                className="block h-full rounded-full bg-in"
                style={{ width: `${busiest > 0 ? (r.total / busiest) * 100 : 0}%` }}
              />
            </span>
            <span className="text-xs whitespace-nowrap tabular-nums">
              <span className="text-in">↓</span>
              {formatRate(r.rx)} <span className="text-out">↑</span>
              {formatRate(r.tx)}
            </span>
          </li>
        ))}
      </ul>
      {rows.length > COLLAPSED_ROWS && (
        <button
          type="button"
          aria-expanded={expanded}
          onClick={() => setExpanded((v) => !v)}
          className="min-h-9 rounded-lg border border-line text-[13px] text-in hover:bg-inset"
        >
          {expanded ? "Show fewer" : `Show all ${rows.length} interfaces`}
        </button>
      )}
    </CardFrame>
  );
}
