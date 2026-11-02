import { useContext } from "react";
import type { CardProps } from "../registry";
import { stats } from "../metrics/chart";
import { Sparkline } from "../metrics/Charts";
import { cardFreshness, StaleContext } from "../metrics/freshness";
import { CardFrame } from "../metrics/CardFrame";
import { formatMs, formatRatio } from "../metrics/format";
import { firstSeries, firstValue } from "../metrics/promql";
import { useInstantQuery, useRangeQuery } from "../metrics/useMetricQuery";

// Polled by Vantage (integrations/builtin/ping_check): vantage_ping_check_*.
const RTT = "vantage_ping_check_rtt_avg_seconds";
const LOSS = "vantage_ping_check_packet_loss_ratio";

/** Loss in the plain text colour while it is 0, warn above, critical at 100%. */
function Loss({ ratio }: { ratio: number | undefined }) {
  // A past value never looks good or bad as if it were current.
  const stale = useContext(StaleContext);
  const tone =
    stale || ratio === undefined || ratio <= 0 ? "" : ratio >= 1 ? "text-crit-text" : "text-warn";
  return (
    <span className="flex items-baseline gap-1 text-sm tabular-nums">
      <span className={tone}>{formatRatio(ratio)}</span>
      <span className="text-xs text-muted">loss</span>
    </span>
  );
}

export function PingCheckCard({ instanceId, title, instance }: CardProps) {
  const rtt = useInstantQuery(instanceId, (sel) => `${RTT}${sel()}`);
  const loss = useInstantQuery(instanceId, (sel) => `${LOSS}${sel()}`);
  const rttHour = useRangeQuery(instanceId, (sel) => `${RTT}${sel()}`);

  const rttValue = firstValue(rtt.data);
  const lossValue = firstValue(loss.data);
  const points = firstSeries(rttHour.data);
  const hour = stats(points);
  const freshness = cardFreshness(rtt, loss);
  return (
    <CardFrame
      title={title}
      showTitle={false}
      instance={instance}
      loading={rtt.isPending || loss.isPending}
      error={rtt.error ?? loss.error}
      freshness={freshness}
      problem={
        freshness.kind === "fresh" && lossValue !== undefined && lossValue >= 1
          ? "100% LOSS"
          : undefined
      }
      empty={rttValue === undefined && lossValue === undefined}
    >
      <div className="flex items-baseline gap-4" role="group" aria-label="Round trip">
        <span className="text-[28px] leading-tight font-medium tracking-tight tabular-nums">
          {formatMs(rttValue)}
        </span>
        <Loss ratio={lossValue} />
      </div>
      {points.length > 1 && (
        <div className="flex flex-col gap-1.5">
          <Sparkline points={points} label="Round trip time, last hour" />
          <div className="flex justify-between gap-2 text-xs text-muted">
            <span>last hour</span>
            {hour && (
              <span className="tabular-nums">
                min {formatMs(hour.min)} · avg {formatMs(hour.avg)} · max {formatMs(hour.max)}
              </span>
            )}
          </div>
        </div>
      )}
    </CardFrame>
  );
}
