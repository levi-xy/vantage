import type { ReactNode } from "react";
import type { Instance } from "../../api";
import { StatusBadge } from "../PlaceholderCard";

export interface CardFrameProps {
  title: string;
  instance: Instance | undefined;
  /** The card's main queries are still loading for the first time. */
  loading: boolean;
  /** A main query failed (the message is the API's, never a secret). */
  error: Error | null;
  /** The queries succeeded but returned nothing. */
  empty: boolean;
  /** Shown when `empty`: what to check. */
  emptyHint?: string;
  children: ReactNode;
}

/** Title, instance name and the shared loading / error / no-data states of a card. */
export function CardFrame({
  title,
  instance,
  loading,
  error,
  empty,
  emptyHint = "No data yet. Prometheus may not have scraped this device.",
  children,
}: CardFrameProps) {
  let body: ReactNode = children;
  if (error) {
    body = (
      <p role="alert" className="text-sm text-red-700 dark:text-red-400">
        Could not load metrics: {error.message}
      </p>
    );
  } else if (loading) {
    body = <p className="text-sm text-slate-500 dark:text-slate-400">Loading…</p>;
  } else if (empty) {
    body = <p className="text-sm text-slate-500 dark:text-slate-400">{emptyHint}</p>;
  }
  return (
    <div className="flex h-full flex-col gap-2 overflow-auto p-4">
      <div className="flex items-baseline justify-between gap-2">
        <span className="text-xs tracking-wide text-slate-500 uppercase dark:text-slate-400">
          {title}
        </span>
        <span className="truncate text-sm font-medium">{instance?.name ?? "Unknown device"}</span>
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <StatusBadge instance={instance} />
      </div>
      {body}
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
  const color = clamped >= 90 ? "bg-red-500" : clamped >= 75 ? "bg-amber-500" : "bg-blue-500";
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
        className="h-1.5 rounded bg-slate-200 dark:bg-slate-800"
      >
        <div className={`h-full rounded ${color}`} style={{ width: `${clamped}%` }} />
      </div>
    </div>
  );
}
