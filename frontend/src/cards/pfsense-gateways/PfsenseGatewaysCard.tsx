import type { CardProps } from "../registry";
import { cardFreshness } from "../metrics/freshness";
import { CardFrame } from "../metrics/CardFrame";
import { formatMs, formatPercent } from "../metrics/format";
import { byLabel } from "../metrics/promql";
import { useInstantQuery } from "../metrics/useMetricQuery";
import { KeyFigure } from "../metrics/KeyFigure";
import { useCardTier, useFittingRows } from "../metrics/tier";
import { gatewayLabel } from "../pfsense-interfaces/interfaceSettings";

// Telegraf's `ping` input, one target per gateway (label `url`). A gateway with 100% loss
// has no RTT series at all, so loss decides up/down and RTT is shown only when present.
// Healthy rows are plain text; only loss is shown in the critical colour.
export function PfsenseGatewaysCard({ instanceId, title, instance }: CardProps) {
  const loss = useInstantQuery(instanceId, (sel) => `ping_percent_packet_loss${sel()}`);
  const rtt = useInstantQuery(instanceId, (sel) => `ping_average_response_ms${sel()}`);
  const label = gatewayLabel(instance);

  const rttBy = new Map(byLabel(rtt.data, "url").map((r) => [r.key, r.value] as const));
  const gateways = byLabel(loss.data, "url")
    .filter((g) => g.value !== undefined)
    .map((g) => {
      const lossPct = g.value as number;
      const ms = rttBy.get(g.key);
      return {
        url: g.key,
        name: label(g.key),
        lossPct,
        down: lossPct >= 100,
        rttSeconds: ms === undefined ? undefined : ms / 1000,
      };
    })
    .sort((a, b) => a.name.localeCompare(b.name));
  const tier = useCardTier();
  // Below full size the worst gateway comes first (most loss, then slowest): a problem is
  // never the row that doesn't fit.
  const worstFirst = [...gateways].sort(
    (a, b) => b.lossPct - a.lossPct || (b.rttSeconds ?? Infinity) - (a.rttSeconds ?? Infinity),
  );
  const fitting = useFittingRows(28, gateways.length);
  const worst = worstFirst[0];
  // When not all fit, the last row says how many more there are.
  const shown =
    tier === "full"
      ? gateways
      : worstFirst.slice(0, fitting < gateways.length ? Math.max(1, fitting - 1) : fitting);

  return (
    <CardFrame
      title={title}
      instance={instance}
      loading={loss.isPending}
      error={loss.error}
      freshness={cardFreshness(loss, rtt)}
      empty={gateways.length === 0}
      emptyHint="No gateway ping data yet. Add a Telegraf ping target per gateway."
    >
      {tier === "small" && worst ? (
        <KeyFigure
          label={`Worst gateway: ${worst.name}`}
          tone={worst.lossPct > 0 ? "bad" : undefined}
        >
          {worst.down ? "no reply" : formatMs(worst.rttSeconds)} · {formatPercent(worst.lossPct)}
        </KeyFigure>
      ) : (
        <ul aria-label="Gateways" className="flex flex-col gap-2">
          {shown.map((g) => (
            <li key={g.url} className="flex min-w-0 items-baseline gap-2">
              <span className="truncate">{g.name}</span>
              {g.name !== g.url && <span className="truncate text-[11px] text-muted">{g.url}</span>}
              <span
                className={`ml-auto shrink-0 text-[13px] tabular-nums ${
                  g.lossPct > 0 ? "font-semibold text-crit-text" : "text-muted"
                }`}
              >
                {g.down ? "no reply" : formatMs(g.rttSeconds)} · {formatPercent(g.lossPct)} loss
              </span>
            </li>
          ))}
          {shown.length < gateways.length && (
            <li className="text-xs text-muted">{gateways.length - shown.length} more</li>
          )}
        </ul>
      )}
    </CardFrame>
  );
}
