import type { CardProps } from "../registry";
import { CardFrame } from "../metrics/CardFrame";
import { byLabel } from "../metrics/promql";
import { useInstantQuery } from "../metrics/useMetricQuery";

// Polled by Vantage (integrations/builtin/cloudflare): vantage_cloudflare_tunnel_*.
const P = "vantage_cloudflare_tunnel_";

const STATUS_STYLE: Record<string, string> = {
  healthy: "text-emerald-700 dark:text-emerald-400",
  degraded: "text-amber-700 dark:text-amber-400",
  down: "text-red-700 dark:text-red-400",
  inactive: "text-slate-500 dark:text-slate-400",
};

export function CloudflareTunnelsCard({ instanceId, title, instance }: CardProps) {
  const info = useInstantQuery(instanceId, (sel) => `${P}info${sel()}`);
  // Only the current status has value 1.
  const status = useInstantQuery(instanceId, (sel) => `${P}status${sel()} == 1`);
  const connections = useInstantQuery(instanceId, (sel) => `${P}connections${sel()}`);

  const state = new Map(byLabel(status.data, "tunnel_id").map((r) => [r.key, r.labels.status]));
  const conns = new Map(byLabel(connections.data, "tunnel_id").map((r) => [r.key, r.value]));
  const tunnels = byLabel(info.data, "tunnel_id")
    .map((t) => ({ id: t.key, name: t.labels.tunnel_name || t.key }))
    .sort((a, b) => a.name.localeCompare(b.name));

  return (
    <CardFrame
      title={title}
      instance={instance}
      loading={info.isPending}
      error={info.error ?? status.error}
      empty={tunnels.length === 0}
      emptyHint="No tunnels reported. Check that Tunnel status is on and the token can read tunnels."
    >
      <ul className="flex flex-col gap-1.5">
        {tunnels.map((t) => {
          const s = state.get(t.id);
          const n = conns.get(t.id);
          return (
            <li key={t.id} className="flex items-baseline justify-between gap-3 text-sm">
              <span className="truncate font-medium">{t.name}</span>
              <span className="flex shrink-0 gap-3 tabular-nums">
                <span className="text-xs text-slate-500 dark:text-slate-400">
                  {n === undefined ? "–" : n} {n === 1 ? "connection" : "connections"}
                </span>
                <span className={STATUS_STYLE[s ?? ""] ?? ""}>{s ?? "unknown"}</span>
              </span>
            </li>
          );
        })}
      </ul>
    </CardFrame>
  );
}
