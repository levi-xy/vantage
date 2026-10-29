import type { CardProps } from "../registry";
import { CardFrame } from "../metrics/CardFrame";
import { formatRatio } from "../metrics/format";
import { firstSeries, firstValue } from "../metrics/promql";
import { StatTile } from "../metrics/StatTile";
import { useInstantQuery, useRangeQuery } from "../metrics/useMetricQuery";

// Scraped from the connector's own /metrics (native cloudflared names).
const CONNECTIONS = "cloudflared_tunnel_ha_connections";
const REQUESTS = "cloudflared_tunnel_total_requests"; // counter
const ERRORS = "cloudflared_tunnel_request_errors"; // counter

function perSecond(v: number | undefined): string {
  if (v === undefined) return "–";
  return `${v >= 10 ? v.toFixed(0) : v.toFixed(2)}/s`;
}

export function CloudflaredCard({ instanceId, title, instance }: CardProps) {
  const reqExpr = (sel: (m?: string) => string) => `sum(rate(${REQUESTS}${sel()}[5m]))`;
  const errExpr = (sel: (m?: string) => string) => `sum(rate(${ERRORS}${sel()}[5m]))`;
  const connections = useInstantQuery(instanceId, (sel) => `${CONNECTIONS}${sel()}`);
  const requests = useInstantQuery(instanceId, reqExpr);
  const errors = useInstantQuery(instanceId, errExpr);
  const reqSpark = useRangeQuery(instanceId, reqExpr);
  const errSpark = useRangeQuery(instanceId, errExpr);

  const conn = firstValue(connections.data);
  const reqRate = firstValue(requests.data);
  const errRate = firstValue(errors.data);
  const errShare = reqRate && errRate !== undefined ? errRate / reqRate : undefined;
  return (
    <CardFrame
      title={title}
      instance={instance}
      loading={connections.isPending}
      error={connections.error ?? requests.error}
      empty={conn === undefined}
      emptyHint="No tunnel metrics yet. Check that cloudflared runs with --metrics and Prometheus can reach it."
    >
      <div className="grid grid-cols-2 gap-3">
        <StatTile
          label="Edge connections"
          value={conn === undefined ? "–" : String(conn)}
          tone={conn === undefined ? undefined : conn > 0 ? "ok" : "bad"}
        />
        <StatTile
          label="Error share"
          value={formatRatio(errShare)}
          tone={errShare === undefined ? undefined : errShare > 0 ? "bad" : "ok"}
        />
        <StatTile label="Requests" value={perSecond(reqRate)} spark={firstSeries(reqSpark.data)} />
        <StatTile label="Errors" value={perSecond(errRate)} spark={firstSeries(errSpark.data)} />
      </div>
    </CardFrame>
  );
}
