import type { CardProps } from "../registry";
import { CardFrame } from "../metrics/CardFrame";
import { formatMs, formatRatio } from "../metrics/format";
import { firstSeries, firstValue } from "../metrics/promql";
import { StatTile } from "../metrics/StatTile";
import { useInstantQuery, useRangeQuery } from "../metrics/useMetricQuery";

// Polled by Vantage (integrations/builtin/ping_check): vantage_ping_check_*.
const RTT = "vantage_ping_check_rtt_avg_seconds";
const LOSS = "vantage_ping_check_packet_loss_ratio";

export function PingCheckCard({ instanceId, title, instance }: CardProps) {
  const rtt = useInstantQuery(instanceId, (sel) => `${RTT}${sel()}`);
  const loss = useInstantQuery(instanceId, (sel) => `${LOSS}${sel()}`);
  const rttSpark = useRangeQuery(instanceId, (sel) => `${RTT}${sel()}`);
  const lossSpark = useRangeQuery(instanceId, (sel) => `${LOSS}${sel()}`);

  const rttValue = firstValue(rtt.data);
  const lossValue = firstValue(loss.data);
  return (
    <CardFrame
      title={title}
      instance={instance}
      loading={rtt.isPending || loss.isPending}
      error={rtt.error ?? loss.error}
      empty={rttValue === undefined && lossValue === undefined}
    >
      <div className="grid grid-cols-2 gap-3">
        <StatTile
          label="Round trip"
          value={formatMs(rttValue)}
          spark={firstSeries(rttSpark.data)}
        />
        <StatTile
          label="Packet loss"
          value={formatRatio(lossValue)}
          tone={lossValue === undefined ? undefined : lossValue > 0 ? "bad" : "ok"}
          spark={firstSeries(lossSpark.data)}
        />
      </div>
    </CardFrame>
  );
}
