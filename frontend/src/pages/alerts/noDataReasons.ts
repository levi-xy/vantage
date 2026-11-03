/** Why an open alert has no data, in plain words (Alerts page and health status). */
export const NO_DATA_REASONS: Record<string, string> = {
  source_stale: "device not reporting",
  source_down: "device not answering",
  metric_missing: "metric missing",
  query_failed: "query failing",
  prometheus_unreachable: "Prometheus unreachable",
  metrics_not_collected: "metrics not being collected",
};
