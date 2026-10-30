import type { CardPlacement, CardSpec, DashboardLayout } from "../api";

export const GRID_COLUMNS = 12;

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
  let cards = saved.cards.filter((c) =>
    c.instance_id === null
      ? widgetTypes.has(c.card_type)
      : knownInstances.has(c.instance_id) &&
        (!loadedInstances.has(c.instance_id) || offered.has(cardKey(c))),
  );
  if ((saved.version ?? 1) < LAYOUT_VERSION) {
    const row: CardPlacement[] = [];
    let x = 0;
    for (const w of widgets) {
      const [width, height] = w.default_size;
      if (x + width > GRID_COLUMNS) break;
      row.push({
        instance_id: null,
        card_type: w.card_type,
        x,
        y: 0,
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
