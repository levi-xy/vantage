import type { CardProps } from "../registry";
import { CardFrame } from "../metrics/CardFrame";
import { formatMs, formatPercent } from "../metrics/format";
import { byLabel } from "../metrics/promql";
import { useInstantQuery } from "../metrics/useMetricQuery";

// Telegraf's `ping` input, one target per gateway (label `url`). A gateway with 100% loss
// has no RTT series at all, so loss decides up/down and RTT is shown only when present.
type GatewayState = "up" | "degraded" | "down";

const BADGE: Record<GatewayState, string> = {
  up: "bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-300",
  degraded: "bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-300",
  down: "bg-red-100 text-red-800 dark:bg-red-950 dark:text-red-300",
};

export function PfsenseGatewaysCard({ instanceId, title, instance }: CardProps) {
  const loss = useInstantQuery(instanceId, (sel) => `ping_percent_packet_loss${sel()}`);
  const rtt = useInstantQuery(instanceId, (sel) => `ping_average_response_ms${sel()}`);

  const rttBy = new Map(byLabel(rtt.data, "url").map((r) => [r.key, r.value] as const));
  const gateways = byLabel(loss.data, "url")
    .filter((g) => g.value !== undefined)
    .map((g) => {
      const lossPct = g.value as number;
      const state: GatewayState = lossPct >= 100 ? "down" : lossPct > 0 ? "degraded" : "up";
      const ms = rttBy.get(g.key);
      return { url: g.key, lossPct, state, rttSeconds: ms === undefined ? undefined : ms / 1000 };
    })
    .sort((a, b) => a.url.localeCompare(b.url));

  return (
    <CardFrame
      title={title}
      instance={instance}
      loading={loss.isPending}
      error={loss.error}
      empty={gateways.length === 0}
      emptyHint="No gateway ping data yet. Add a Telegraf ping target per gateway."
    >
      <ul className="flex flex-col gap-1.5">
        {gateways.map((g) => (
          <li key={g.url} className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1">
            <span className="truncate font-medium">{g.url}</span>
            <span className="flex items-center gap-2 text-sm tabular-nums">
              <span className="text-slate-600 dark:text-slate-300">
                {g.state === "down" ? "no reply" : formatMs(g.rttSeconds)} ·{" "}
                {formatPercent(g.lossPct)} loss
              </span>
              <span className={`rounded px-1.5 py-0.5 text-xs font-medium ${BADGE[g.state]}`}>
                {g.state}
              </span>
            </span>
          </li>
        ))}
      </ul>
    </CardFrame>
  );
}
