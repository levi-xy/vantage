import type { CardProps } from "../registry";
import { CardFrame } from "../metrics/CardFrame";
import { formatBytes, formatRatio } from "../metrics/format";
import { byLabel, firstValue } from "../metrics/promql";
import { useInstantQuery } from "../metrics/useMetricQuery";

// Polled by Vantage (integrations/builtin/cloudflare): vantage_cloudflare_*. Traffic values are
// totals for the last complete UTC hour, so they are read directly, not with rate().
const P = "vantage_cloudflare_";
const TRAFFIC = "vantage_cloudflare_zone_(requests|cached_requests|bytes|threats)";

function count(v: number | undefined): string {
  return v === undefined ? "–" : Math.round(v).toLocaleString("en");
}

export function CloudflareZonesCard({ instanceId, title, instance }: CardProps) {
  const info = useInstantQuery(instanceId, (sel) => `${P}zone_info${sel()}`);
  // One query for all per-zone traffic series; the metric name tells them apart.
  const traffic = useInstantQuery(instanceId, (sel) => sel(`__name__=~"${TRAFFIC}"`));
  const analyticsUp = useInstantQuery(instanceId, (sel) => `${P}analytics_up${sel()}`);

  const values = new Map<string, number | undefined>();
  for (const row of byLabel(traffic.data, "zone_id")) {
    values.set(`${row.labels.__name__ ?? ""}|${row.key}`, row.value);
  }
  const get = (metric: string, zone: string) => values.get(`${P}zone_${metric}|${zone}`);
  const zones = byLabel(info.data, "zone_id")
    .map((z) => ({ id: z.key, name: z.labels.zone_name || z.key }))
    .sort((a, b) => a.name.localeCompare(b.name));
  const incomplete = firstValue(analyticsUp.data) === 0;

  return (
    <CardFrame
      title={title}
      instance={instance}
      loading={info.isPending}
      error={info.error ?? traffic.error}
      empty={zones.length === 0}
      emptyHint="No zones reported yet. Check the instance status and the API token."
    >
      {incomplete && (
        <p className="text-xs text-amber-700 dark:text-amber-400">
          Analytics incomplete: some zones may be missing figures.
        </p>
      )}
      <ul className="flex flex-col gap-3">
        {zones.map((z) => {
          const requests = get("requests", z.id);
          const cached = get("cached_requests", z.id);
          return (
            <li key={z.id} className="flex flex-col gap-0.5 text-sm">
              <span className="truncate font-medium">{z.name}</span>
              {requests === undefined ? (
                <span className="text-xs text-slate-500 dark:text-slate-400">
                  Analytics unavailable
                </span>
              ) : (
                <dl className="grid grid-cols-2 gap-x-3 text-xs sm:grid-cols-4">
                  <Item label="Requests" value={count(requests)} />
                  <Item label="Bandwidth" value={formatBytes(get("bytes", z.id))} />
                  <Item
                    label="Cached"
                    value={
                      requests > 0 && cached !== undefined ? formatRatio(cached / requests) : "–"
                    }
                  />
                  <Item label="Threats" value={count(get("threats", z.id))} />
                </dl>
              )}
            </li>
          );
        })}
      </ul>
      <p className="text-xs text-slate-500 dark:text-slate-400">Last complete UTC hour</p>
    </CardFrame>
  );
}

function Item({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex justify-between gap-2 sm:flex-col sm:justify-start">
      <dt className="text-slate-500 dark:text-slate-400">{label}</dt>
      <dd className="tabular-nums">{value}</dd>
    </div>
  );
}
