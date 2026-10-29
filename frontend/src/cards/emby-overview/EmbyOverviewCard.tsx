import type { CardProps } from "../registry";
import { CardFrame } from "../metrics/CardFrame";
import { byLabel, firstSeries } from "../metrics/promql";
import { StatTile } from "../metrics/StatTile";
import { useInstantQuery, useRangeQuery } from "../metrics/useMetricQuery";

// Polled by Vantage (integrations/builtin/emby): vantage_emby_*. Counts only: no user names or
// media titles exist in the metrics.
const SCALARS =
  "vantage_emby_(sessions|now_playing|update_available|restart_pending)|vantage_instance_up";
const METHODS: [string, string][] = [
  ["direct_play", "Direct play"],
  ["direct_stream", "Direct stream"],
  ["transcode", "Transcode"],
];

const num = (v: number | undefined) => (v === undefined || Number.isNaN(v) ? "–" : String(v));

export function EmbyOverviewCard({ instanceId, title, instance }: CardProps) {
  // One query for the scalar series; the metric name tells them apart.
  const scalars = useInstantQuery(instanceId, (sel) => sel(`__name__=~"${SCALARS}"`));
  const info = useInstantQuery(instanceId, (sel) => `vantage_emby_server_info${sel()}`);
  const streams = useInstantQuery(instanceId, (sel) => `vantage_emby_streams${sel()}`);
  const playingSpark = useRangeQuery(instanceId, (sel) => `vantage_emby_now_playing${sel()}`);

  const value = new Map(
    (scalars.data?.series ?? []).map((s) => [s.labels.__name__ ?? "", Number(s.values[0]?.[1])]),
  );
  const up = value.get("vantage_instance_up");
  const serverInfo = info.data?.series[0]?.labels;
  const method = new Map(byLabel(streams.data, "play_method").map((r) => [r.key, r.value]));

  return (
    <CardFrame
      title={title}
      instance={instance}
      loading={scalars.isPending}
      error={scalars.error ?? streams.error}
      empty={value.size === 0}
      emptyHint="No Emby metrics yet. Check the instance status and the API key."
    >
      <div className="flex flex-wrap items-baseline gap-x-3 text-sm">
        <span
          className={
            up === 1 ? "text-emerald-700 dark:text-emerald-400" : "text-red-700 dark:text-red-400"
          }
        >
          {up === 1 ? "Server up" : "Server down"}
        </span>
        {serverInfo?.server_name && (
          <span className="truncate font-medium">{serverInfo.server_name}</span>
        )}
        {serverInfo?.version && (
          <span className="text-xs text-slate-500 dark:text-slate-400">v{serverInfo.version}</span>
        )}
      </div>
      <div className="grid grid-cols-2 gap-3">
        <StatTile
          label="Now playing"
          value={num(value.get("vantage_emby_now_playing"))}
          spark={firstSeries(playingSpark.data)}
        />
        <StatTile label="Active sessions" value={num(value.get("vantage_emby_sessions"))} />
      </div>
      <div className="grid grid-cols-3 gap-3">
        {METHODS.map(([key, label]) => (
          <StatTile key={key} label={label} value={num(method.get(key))} />
        ))}
      </div>
      {value.get("vantage_emby_update_available") === 1 && (
        <p className="text-xs text-amber-700 dark:text-amber-400">Update available</p>
      )}
      {value.get("vantage_emby_restart_pending") === 1 && (
        <p className="text-xs text-amber-700 dark:text-amber-400">Restart pending</p>
      )}
    </CardFrame>
  );
}
