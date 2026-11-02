import { Link } from "react-router";
import { useCardTier } from "./tier";

export interface KpiTileProps {
  /** Also the tile's accessible name. */
  label: string;
  /** Already formatted. */
  value: string;
  /** Small text after the value ("up", "firing"). */
  unit?: string;
  note?: string;
  /** "bad" shows the value in the critical colour. Healthy stays the plain text colour. */
  tone?: "bad";
  /** Makes the whole tile a link. */
  to?: string;
}

const MUTED = "text-muted";
const BAD = "text-crit-text";
const LAYOUT =
  "flex h-full min-w-0 flex-col justify-center gap-0.5 px-3.5 py-3 md:gap-1.5 md:px-4.5 md:py-4";
// A 1-row tile: the label and the value, no note.
const SMALL_LAYOUT = "flex h-full min-w-0 flex-col justify-center gap-0.5 px-3 py-2";

/**
 * A summary tile: small label, big number, small note. Fills its grid card. Small (1 row):
 * the label and the value only; a problem still shows in the value's colour.
 */
export function KpiTile({ label, value, unit, note, tone, to }: KpiTileProps) {
  const small = useCardTier() === "small";
  const body = (
    <>
      <span className={`truncate text-xs font-medium ${small ? "leading-4" : ""} ${MUTED}`}>
        {label}
      </span>
      <span className="flex min-w-0 items-baseline gap-2">
        <span
          className={`truncate font-medium tracking-tight tabular-nums ${
            small ? "text-base leading-5" : "text-[22px] leading-tight md:text-[26px]"
          } ${tone === "bad" ? BAD : ""}`}
        >
          {value}
        </span>
        {unit && <span className={`text-[13px] whitespace-nowrap ${MUTED}`}>{unit}</span>}
      </span>
      {note && !small && (
        <span className={`truncate text-xs ${MUTED}`} title={note}>
          {note}
        </span>
      )}
    </>
  );
  return (
    <div role="group" aria-label={label} className="h-full min-w-0">
      {to ? (
        <Link to={to} className={`${small ? SMALL_LAYOUT : LAYOUT} hover:bg-inset`}>
          {body}
        </Link>
      ) : (
        <div className={small ? SMALL_LAYOUT : LAYOUT}>{body}</div>
      )}
    </div>
  );
}
