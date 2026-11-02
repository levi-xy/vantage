import { useEffect, useState } from "react";
import { liveStatus } from "./live";
import type { LiveState, LiveTone } from "./live";

const RING = 2 * Math.PI * 5.5;

const STROKE: Record<LiveTone, string> = {
  connecting: "stroke-muted",
  live: "stroke-ok",
  stale: "stroke-warn",
  offline: "stroke-crit",
};
const FILL: Record<LiveTone, string> = {
  connecting: "fill-muted",
  live: "fill-ok",
  stale: "fill-warn",
  offline: "fill-crit",
};
const TEXT: Record<LiveTone, string> = {
  connecting: "text-muted",
  live: "text-muted",
  stale: "text-warn",
  offline: "text-crit-text",
};

const TITLE =
  "The ring fills until the next refresh. Amber when data is stale, red when the backend is unreachable.";

/** A ring that fills over the refresh interval, and the time of the last data update. */
export function LiveIndicator({
  connection,
  lastUpdate,
}: {
  connection: LiveState;
  lastUpdate: number | null;
}) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1_000);
    return () => clearInterval(timer);
  }, []);
  const status = liveStatus(connection, lastUpdate, now);

  return (
    <span
      data-tone={status.tone}
      title={TITLE}
      className={`flex items-center gap-[7px] text-[13px] whitespace-nowrap ${TEXT[status.tone]}`}
    >
      <svg width="14" height="14" viewBox="0 0 14 14" fill="none" aria-hidden="true">
        <circle cx="7" cy="7" r="5.5" strokeWidth="2" className="stroke-track" />
        {status.progress > 0 && (
          <circle
            cx="7"
            cy="7"
            r="5.5"
            strokeWidth="2"
            strokeLinecap="round"
            strokeDasharray={`${(status.progress * RING).toFixed(2)} ${RING.toFixed(2)}`}
            transform="rotate(-90 7 7)"
            className={`${STROKE[status.tone]} transition-[stroke-dasharray] duration-1000 ease-linear motion-reduce:transition-none`}
          />
        )}
        <circle cx="7" cy="7" r="2" className={FILL[status.tone]} />
      </svg>
      {status.label}
    </span>
  );
}
