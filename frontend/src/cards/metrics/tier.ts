// Card tiers: how much a card shows, from its size on the dashboard grid. Users can make any
// card as small as 2 x 1 and trade detail for space; a card never scrolls inside.
//   full:   everything (the card is at least its full size, the old minimum).
//   medium: the header and the 2 or 3 key figures; no charts, sparklines or long lists.
//   small:  the device name, its state and one key figure.
// Outside the desktop grid (phone list, tests) there is no tier context: always full.

import { createContext, useContext } from "react";

export type CardTier = "full" | "medium" | "small";

/** Every card can be made this small (grid width, height). */
export const SMALLEST_SIZE: [number, number] = [2, 1];
/** From this size on (and below the full size) a card shows its medium tier. */
export const MEDIUM_SIZE: [number, number] = [3, 2];

export function tierFor(size: [number, number], full: [number, number]): CardTier {
  const [w, h] = size;
  if (w >= full[0] && h >= full[1]) return "full";
  if (w >= MEDIUM_SIZE[0] && h >= MEDIUM_SIZE[1]) return "medium";
  return "small";
}

export interface TierValue {
  tier: CardTier;
  /** The card's height in px on the grid, when known. */
  heightPx?: number;
}

export const CardTierContext = createContext<TierValue>({ tier: "full" });

export const useCardTier = (): CardTier => useContext(CardTierContext).tier;

// A medium card's padding (2 x 12), header (34) and the gap under it (8).
const MEDIUM_CHROME_PX = 66;

/**
 * How many list rows of `rowPx` (row plus gap) fit under a medium card's header: at least 1,
 * at most `max`. Unknown height: `max`.
 */
export function useFittingRows(rowPx: number, max: number): number {
  const { heightPx } = useContext(CardTierContext);
  if (heightPx === undefined) return max;
  return Math.max(1, Math.min(max, Math.floor((heightPx - MEDIUM_CHROME_PX) / rowPx)));
}

// A row of figures (label and value) and the gap under it.
const FIGURES_PX = 54;

/**
 * Whether a medium card has room for one more line of `linePx` under its row of figures.
 * Only extra detail goes there: a problem belongs in a figure or the header, which always fit.
 */
export function useRoomUnderFigures(linePx = 20): boolean {
  const { heightPx } = useContext(CardTierContext);
  return heightPx === undefined || heightPx - MEDIUM_CHROME_PX - FIGURES_PX >= linePx;
}
