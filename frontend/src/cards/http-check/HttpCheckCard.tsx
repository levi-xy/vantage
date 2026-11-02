import type { CardProps } from "../registry";
import { cardFreshness } from "../metrics/freshness";
import { CardFrame } from "../metrics/CardFrame";
import { formatMs } from "../metrics/format";
import { firstSeries, firstValue } from "../metrics/promql";
import { Figures, KeyFigure } from "../metrics/KeyFigure";
import { StatTile } from "../metrics/StatTile";
import { useCardTier } from "../metrics/tier";
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
  const tier = useCardTier();
  const ms = formatMs(firstValue(duration.data));
  return (
    <CardFrame
      title={title}
      showTitle={false}
      instance={instance}
      loading={success.isPending}
      error={success.error ?? status.error ?? duration.error}
      freshness={cardFreshness(success, status, duration)}
      empty={up === undefined}
    >
      {tier === "small" ? (
        // A failed check is the problem here, so it leads the line.
        <KeyFigure
          label="Result, status code and response time"
          tone={up === 1 ? undefined : "bad"}
        >
          {[up === 1 ? "Up" : "Down", code ? String(code) : "", up === 1 ? ms : ""]
            .filter(Boolean)
            .join(" · ")}
        </KeyFigure>
      ) : (
        <Figures>
          <StatTile
            label="Result"
            value={up === 1 ? "Up" : "Down"}
            tone={up === 1 ? "ok" : "bad"}
          />
          {/* 0 = no HTTP response (connection failed): there is no status code to show. */}
          <StatTile label="Status code" value={code ? String(code) : "–"} />
          <StatTile
            label="Response time"
            value={ms}
            spark={tier === "full" ? firstSeries(spark.data) : undefined}
          />
        </Figures>
      )}
    </CardFrame>
  );
}
