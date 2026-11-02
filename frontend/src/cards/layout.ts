import type { CardPlacement, CardSpec, DashboardLayout } from "../api";

export const GRID_COLUMNS = 12;
/** One grid row, and the gap between cards, in px. A 1-row card fits a name, state and figure. */
export const ROW_HEIGHT = 56;
export const GRID_MARGIN = 16;

/** The height in px of a card `rows` grid rows tall. */
export const gridHeightPx = (rows: number) => rows * ROW_HEIGHT + (rows - 1) * GRID_MARGIN;

// Below this width the grid is one fixed column, and the dashboard isn't editable.
export const DESKTOP_MIN_WIDTH = 768;

/** Layout version written by this frontend. 2 = dashboard-wide cards are ordinary cards. */
export const LAYOUT_VERSION = 2;

/** A card an instance offers. */
export interface AvailableCard {
  instanceId: string;
  spec: CardSpec;
}

/** A dashboard-wide card ("Devices healthy" and the like): it belongs to no instance. */
export interface WidgetSpec {
  card_type: string;
  title: string;
  default_size: [number, number];
  /** Added after layout version 2: never put on an old layout's former fixed row. */
  notOnOldLayouts?: boolean;
}

type Keyed = { instance_id: string | null; card_type: string };

export const cardKey = (c: Keyed) => `${c.instance_id ?? "dashboard"}:${c.card_type}`;

const clamp = (n: number, min: number, max: number) => Math.min(Math.max(Math.trunc(n), min), max);

/** The bottom edge of a layout, in grid rows. */
const bottom = (cards: CardPlacement[]) => cards.reduce((b, c) => Math.max(b, c.y + c.h), 0);

/**
 * Bring a saved layout in line with what exists now. Nothing is ever added on its own (the
 * admin adds cards in edit mode), except once: a layout older than version 2 gets the
 * dashboard-wide cards on a row above everything, since they used to be a fixed row.
 *
 * Cards of deleted instances are dropped. A card whose instance's card list loaded and no
 * longer offers its type is dropped too; a failed card-list request never prunes.
 */
export function prepareLayout(
  saved: DashboardLayout,
  widgets: readonly WidgetSpec[],
  available: AvailableCard[],
  knownInstances: ReadonlySet<string>,
  loadedInstances: ReadonlySet<string>,
): CardPlacement[] {
  const offered = new Set(available.map((a) => cardKey({ instance_id: a.instanceId, ...a.spec })));
  const widgetTypes = new Set(widgets.map((w) => w.card_type));
  // A card is shown once: a layout holding the same card twice (an old layout that already
  // had a dashboard-wide card, or a bad save) drew both copies on a phone, and on the grid
  // the hidden second copy left a hole. The first one wins.
  const seen = new Set<string>();
  let cards = saved.cards.filter((c) => {
    const key = cardKey(c);
    if (seen.has(key)) return false;
    seen.add(key);
    return c.instance_id === null
      ? widgetTypes.has(c.card_type)
      : knownInstances.has(c.instance_id) &&
          (!loadedInstances.has(c.instance_id) || offered.has(key));
  });
  if ((saved.version ?? 1) < LAYOUT_VERSION) {
    const row: CardPlacement[] = [];
    let x = 0;
    let y = 0;
    for (const w of widgets) {
      if (w.notOnOldLayouts) continue;
      if (seen.has(cardKey({ instance_id: null, card_type: w.card_type }))) continue;
      const [width, height] = w.default_size;
      // A card that doesn't fit on the line starts the next one; wider than the grid: left out.
      if (width > GRID_COLUMNS) continue;
      if (x + width > GRID_COLUMNS) {
        x = 0;
        y = bottom(row);
      }
      row.push({
        instance_id: null,
        card_type: w.card_type,
        x,
        y,
        w: width,
        h: height,
        settings: {},
      });
      x += width;
    }
    const shift = bottom(row);
    cards = [...row, ...cards.map((c) => ({ ...c, y: c.y + shift }))];
  }
  return cards;
}

/** `card` added at the bottom left of `cards`, at the given size. */
export function addCard(
  cards: CardPlacement[],
  card: Keyed,
  size: [number, number],
): CardPlacement[] {
  const w = clamp(size[0], 1, GRID_COLUMNS);
  const h = clamp(size[1], 1, 100);
  return [...cards, { ...card, x: 0, y: bottom(cards), w, h, settings: {} }];
}
