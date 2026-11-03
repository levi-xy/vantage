import { useQuery } from "@tanstack/react-query";
import { useApiClient } from "../../app/session";
import { useNow } from "../../pages/alerts/tone";
import type { CardProps } from "../registry";
import { CardFrame } from "../metrics/CardFrame";
import { FRESH } from "../metrics/freshness";
import type { Freshness } from "../metrics/freshness";
import { KeyFigure } from "../metrics/KeyFigure";
import { useCardTier } from "../metrics/tier";

// Who is playing what on an Emby server. Not a metric: the backend keeps the latest poll's
// list in memory (GET /instances/{id}/now-playing), so there is no history to chart.
const REFRESH_MS = 15_000;
// Older than this, the list is shown greyed as of its time (Emby is polled every 60 s).
const STALE_AFTER_MS = 210_000;
// Rows that fit the card at its full size (cards never scroll); the rest are counted.
const MAX_ROWS = 4;

export function EmbyNowPlayingCard({ instanceId, title, instance }: CardProps) {
  const api = useApiClient();
  const tier = useCardTier();
  const query = useQuery({
    queryKey: ["instances", instanceId, "now-playing"],
    queryFn: () => api.nowPlaying(instanceId),
    refetchInterval: REFRESH_MS,
  });
  const now = useNow();
  const data = query.data;
  const checked = data?.checked_at ? Date.parse(data.checked_at) : null;
  const freshness: Freshness =
    checked !== null && now - checked > STALE_AFTER_MS
      ? { kind: "stale", asOf: checked / 1000 }
      : FRESH;
  const sessions = data?.sessions ?? [];

  return (
    <CardFrame
      title={title}
      instance={instance}
      loading={query.isPending}
      error={query.error}
      freshness={freshness}
      empty={checked === null}
      emptyHint="Not polled yet. The list appears after Vantage's next poll of this server."
    >
      {tier !== "full" ? (
        <KeyFigure label="Now playing">
          {sessions.length === 0
            ? "Nothing playing"
            : sessions.length === 1
              ? `${sessions[0]?.user || "Someone"}: ${sessions[0]?.title}`
              : `${sessions.length} playing`}
        </KeyFigure>
      ) : sessions.length === 0 ? (
        <p className="text-sm text-muted">Nothing playing.</p>
      ) : (
        <ul aria-label="Now playing" className="min-h-0 divide-y divide-line">
          {sessions.slice(0, MAX_ROWS).map((s, i) => (
            <li key={i} className="flex min-w-0 items-baseline gap-2 py-1.5 text-sm">
              <span className="w-28 shrink-0 truncate font-medium text-device" title={s.user}>
                {s.user || "Unknown user"}
              </span>
              <span className="min-w-0 flex-1 truncate" title={s.title}>
                {s.title}
              </span>
            </li>
          ))}
          {sessions.length > MAX_ROWS && (
            <li className="py-1.5 text-xs text-muted">and {sessions.length - MAX_ROWS} more</li>
          )}
        </ul>
      )}
    </CardFrame>
  );
}
