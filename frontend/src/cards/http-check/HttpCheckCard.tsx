import type { CardProps } from "../registry";
import { CardFrame } from "../metrics/CardFrame";
import { formatMs } from "../metrics/format";
import { firstSeries, firstValue } from "../metrics/promql";
import { StatTile } from "../metrics/StatTile";
import { useInstantQuery, useRangeQuery } from "../metrics/useMetricQuery";

// Polled by Vantage (integrations/builtin/http_check): vantage_http_check_*.
// The integration exposes no certificate expiry, so the card shows none.
const SUCCESS = "vantage_http_check_success";
const STATUS = "vantage_http_check_status_code";
const DURATION = "vantage_http_check_duration_seconds";

export function HttpCheckCard({ instanceId, title, instance }: CardProps) {
  const success = useInstantQuery(instanceId, (sel) => `${SUCCESS}${sel()}`);
  const status = useInstantQuery(instanceId, (sel) => `${STATUS}${sel()}`);
  const duration = useInstantQuery(instanceId, (sel) => `${DURATION}${sel()}`);
  const spark = useRangeQuery(instanceId, (sel) => `${DURATION}${sel()}`);

  const up = firstValue(success.data);
  const code = firstValue(status.data);
  return (
    <CardFrame
      title={title}
      instance={instance}
      loading={success.isPending}
      error={success.error ?? status.error ?? duration.error}
      empty={up === undefined}
    >
      <div className="grid grid-cols-3 gap-3">
        <StatTile label="Result" value={up === 1 ? "Up" : "Down"} tone={up === 1 ? "ok" : "bad"} />
        {/* 0 = no HTTP response (connection failed): there is no status code to show. */}
        <StatTile label="Status code" value={code ? String(code) : "–"} />
        <StatTile
          label="Response time"
          value={formatMs(firstValue(duration.data))}
          spark={firstSeries(spark.data)}
        />
      </div>
    </CardFrame>
  );
}
