// Small pieces the SNMP port cards share.
import { LEGEND, TILE } from "./ports";

/** A small sample tile per state: "Link up", "No link", "Disabled". */
export function PortLegend() {
  return (
    <div aria-label="Legend" className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted">
      {LEGEND.map(({ state, label }) => (
        <span key={state} className="flex items-center gap-1.5">
          <span
            aria-hidden="true"
            className={`h-2.5 w-3 rounded-[2px] border-[1.5px] ${TILE[state]}`}
          />
          {label}
        </span>
      ))}
    </div>
  );
}

/**
 * "2 internal interfaces hidden · Show": shown only when there are any. The counts next to
 * it are of physical ports only.
 */
export function InternalToggle({
  count,
  shown,
  onToggle,
}: {
  count: number;
  shown: boolean;
  onToggle: () => void;
}) {
  if (count === 0) return null;
  const what = count === 1 ? "1 internal interface" : `${count} internal interfaces`;
  return (
    <p className="text-xs text-muted">
      {shown ? `${what} shown, not counted` : `${what} hidden`} ·{" "}
      <button type="button" onClick={onToggle} className="underline hover:text-ink">
        {shown ? "Hide" : "Show"}
      </button>
    </p>
  );
}
