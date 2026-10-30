import type { CardProps } from "../registry";
import { CardFrame } from "../metrics/CardFrame";
import { formatBitsPerSecond } from "../metrics/format";
import { byLabel } from "../metrics/promql";
import { useInstantQuery } from "../metrics/useMetricQuery";
import { interfaceSettings, REAL_IF } from "./interfaceSettings";

/** Traffic per interface, under the names set in the pfSense device settings. */
export function PfsenseInterfacesCard({ instanceId, title, instance }: CardProps) {
  const rx = useInstantQuery(instanceId, (sel) => `8 * rate(net_bytes_recv${sel(REAL_IF)}[5m])`);
  const tx = useInstantQuery(instanceId, (sel) => `8 * rate(net_bytes_sent${sel(REAL_IF)}[5m])`);
  const settings = interfaceSettings(instance);

  const txBy = new Map(byLabel(tx.data, "interface").map((r) => [r.key, r.value] as const));
  const rows = byLabel(rx.data, "interface")
    .filter((r) => !settings.hidden(r.key))
    .map((r) => ({ id: r.key, name: settings.label(r.key), rx: r.value, tx: txBy.get(r.key) }))
    .sort((a, b) => a.name.localeCompare(b.name));

  return (
    <CardFrame
      title={title}
      instance={instance}
      loading={rx.isPending}
      error={rx.error ?? tx.error}
      empty={rows.length === 0}
    >
      <section aria-label="Interfaces" className="flex flex-col gap-1 text-sm">
        {rows.map((r) => (
          <div key={r.id} className="flex flex-wrap justify-between gap-x-3">
            <span className="font-medium" title={r.name === r.id ? undefined : r.id}>
              {r.name}
            </span>
            <span className="text-slate-600 tabular-nums dark:text-slate-300">
              ↓ {formatBitsPerSecond(r.rx)} · ↑ {formatBitsPerSecond(r.tx)}
            </span>
          </div>
        ))}
      </section>
    </CardFrame>
  );
}
