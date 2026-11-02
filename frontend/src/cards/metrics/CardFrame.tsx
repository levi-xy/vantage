import { useContext } from "react";
import type { ReactNode } from "react";
import type { Instance } from "../../api";
import { CardHeader } from "./CardHeader";
import { FRESH, formatAsOf, StaleContext } from "./freshness";
import type { Freshness } from "./freshness";

export interface CardFrameProps {
  title: string;
  /**
   * Show `title` as a small section title under the header. Off for a device's main card,
   * where the header already says what it is.
   */
  showTitle?: boolean;
  instance: Instance | undefined;
  /** The card's main queries are still loading for the first time. */
  loading: boolean;
  /** A main query failed (the message is the API's, never a secret). */
  error: Error | null;
  /** The queries succeeded but returned nothing. */
  empty: boolean;
  /** Shown when `empty`: what to check. */
  emptyHint?: string;
  /** How old the shown values are: `cardFreshness(...)` of the card's instant queries. */
  freshness?: Freshness;
  /** The device's uptime, for the header's subtitle. */
  uptimeSeconds?: number;
  /** A card-specific problem for the header's state ("100% LOSS"). */
  problem?: string;
  children: ReactNode;
}

/** The shared header, the section title and the loading / error / no-data states of a card. */
export function CardFrame({
  title,
  showTitle = true,
  instance,
  loading,
  error,
  empty,
  emptyHint = "No data yet. Prometheus may not have scraped this device.",
  freshness = FRESH,
  uptimeSeconds,
  problem,
  children,
}: CardFrameProps) {
  const stale = freshness.kind === "stale";
  let body: ReactNode = stale ? (
    // A past value: greyed, and never green or red as if it were current.
    <div data-stale="true" className="flex flex-col gap-3 opacity-60 grayscale">
      <StaleContext.Provider value>{children}</StaleContext.Provider>
    </div>
  ) : (
    children
  );
  if (error) {
    body = (
      <p role="alert" className="line-clamp-3 text-sm text-crit-text" title={error.message}>
        Could not load metrics: {error.message}
      </p>
    );
  } else if (loading) {
    body = <p className="text-sm text-muted">Loading…</p>;
  } else if (empty) {
    const hint = freshness.kind === "missing" ? freshness.note : emptyHint;
    body = <p className="text-sm text-muted">{hint}</p>;
  }
  return (
    <div className="flex h-full min-w-0 flex-col gap-3 overflow-auto p-4">
      <CardHeader
        instance={instance}
        freshness={error ? FRESH : freshness}
        uptimeSeconds={uptimeSeconds}
        problem={problem}
      />
      {showTitle && <h3 className="text-xs font-medium text-muted">{title}</h3>}
      {body}
      {stale && !error && (
        <p className="text-xs text-muted">Values as of {formatAsOf(freshness.asOf)}, greyed.</p>
      )}
    </div>
  );
}

/** A labelled 0 to 100 bar, for disk and memory usage. */
export function UsageBar({
  label,
  percent,
  text,
}: {
  label: string;
  percent: number;
  text: string;
}) {
  const clamped = Math.min(100, Math.max(0, percent));
  const stale = useContext(StaleContext);
  const color = stale
    ? "bg-slate-400 dark:bg-slate-500"
    : clamped >= 90
      ? "bg-crit"
      : clamped >= 75
        ? "bg-warn"
        : "bg-in";
  return (
    <div className="flex flex-col gap-0.5">
      <div className="flex justify-between gap-2 text-xs">
        <span className="truncate">{label}</span>
        <span className="tabular-nums">{text}</span>
      </div>
      <div
        role="progressbar"
        aria-label={label}
        aria-valuenow={Math.round(clamped)}
        aria-valuemin={0}
        aria-valuemax={100}
        className="h-1.5 rounded bg-track"
      >
        <div className={`h-full rounded ${color}`} style={{ width: `${clamped}%` }} />
      </div>
    </div>
  );
}
