import type { CardProps } from "../registry";
import { CardFrame } from "../metrics/CardFrame";
import { byLabel } from "../metrics/promql";
import { useInstantQuery } from "../metrics/useMetricQuery";

// Polled by Vantage (integrations/builtin/emby). Library and item names are never exported,
// only counts by kind.
function label(key: string): string {
  const text = key.replace(/_/g, " ");
  return text.charAt(0).toUpperCase() + text.slice(1);
}

export function EmbyLibrariesCard({ instanceId, title, instance }: CardProps) {
  const items = useInstantQuery(instanceId, (sel) => `vantage_emby_items${sel()}`);
  const libraries = useInstantQuery(instanceId, (sel) => `vantage_emby_libraries${sel()}`);

  const itemRows = byLabel(items.data, "item_type")
    .filter((r) => r.value !== undefined)
    .sort((a, b) => (b.value ?? 0) - (a.value ?? 0));
  const libRows = byLabel(libraries.data, "library_type").sort((a, b) =>
    a.key.localeCompare(b.key),
  );

  return (
    <CardFrame
      title={title}
      instance={instance}
      loading={items.isPending}
      error={items.error ?? libraries.error}
      empty={itemRows.length === 0 && libRows.length === 0}
      emptyHint="No library counts. The API key may not be allowed to read them."
    >
      <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-sm sm:grid-cols-3">
        {itemRows.map((r) => (
          <div key={r.key} className="flex justify-between gap-2">
            <dt className="truncate text-slate-500 dark:text-slate-400">{label(r.key)}</dt>
            <dd className="tabular-nums">{Math.round(r.value ?? 0).toLocaleString("en")}</dd>
          </div>
        ))}
      </dl>
      {libRows.length > 0 && (
        <p className="text-xs text-slate-500 dark:text-slate-400">
          Libraries:{" "}
          {libRows.map((r) => `${r.value ?? 0} ${label(r.key).toLowerCase()}`).join(", ")}
        </p>
      )}
    </CardFrame>
  );
}
