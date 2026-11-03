// The components the Alerts page's sections share: a section heading and a plain bordered
// tag (as in the health popover).

import type { ReactNode } from "react";
import { toneText } from "./tone";
import type { Tone } from "./tone";

/** "FIRING", "NO DATA": a small outlined tag in the row's tone. */
export function Tag({ tone, children }: { tone: Tone; children: ReactNode }) {
  return (
    <span
      className={`inline-block shrink-0 rounded border border-current px-1.5 py-0.5 text-[11px] leading-none font-medium tracking-wide whitespace-nowrap ${toneText(tone)}`}
    >
      {children}
    </span>
  );
}

/** A section's heading, a muted count and an optional action on the right. */
export function SectionHeader({
  id,
  title,
  count,
  children,
}: {
  id: string;
  title: string;
  count?: number;
  children?: ReactNode;
}) {
  return (
    <div className="flex items-center justify-between gap-3">
      <h2 id={id} className="flex items-baseline gap-2 text-[15px] font-semibold">
        {title}
        {count !== undefined && (
          <span className="text-sm font-normal text-muted tabular-nums">{count}</span>
        )}
      </h2>
      {children}
    </div>
  );
}
