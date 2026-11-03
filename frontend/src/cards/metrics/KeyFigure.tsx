import { useContext } from "react";
import type { ReactNode } from "react";
import { StaleContext } from "./freshness";

/**
 * The one line a small card shows under its name. `tone` colours it, except for a past
 * (stale) value, which never looks good or bad as if it were current.
 */
export function KeyFigure({
  label,
  tone,
  children,
}: {
  /** What the figure is ("Worst gateway"): its accessible name and tooltip. */
  label: string;
  tone?: "bad" | "warn";
  children: ReactNode;
}) {
  const stale = useContext(StaleContext);
  const color = stale || !tone ? "" : tone === "bad" ? "font-semibold text-crit-text" : "text-warn";
  return (
    <div
      role="group"
      aria-label={label}
      title={label}
      className={`truncate text-[13px] leading-[18px] font-medium tabular-nums ${color}`}
    >
      {children}
    </div>
  );
}

/** A medium card's row of 2 or 3 figures. */
export function Figures({ children }: { children: ReactNode }) {
  return <div className="grid auto-cols-fr grid-flow-col gap-3">{children}</div>;
}
