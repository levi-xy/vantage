import type { CardPlacement, CardSpec } from "../api";

export const GRID_COLUMNS = 12;

/** A card an instance offers: the unit the dashboard layout is kept in sync with. */
export interface AvailableCard {
  instanceId: string;
  spec: CardSpec;
}

export const cardKey = (c: { instance_id: string; card_type: string }) =>
  `${c.instance_id}:${c.card_type}`;

const clamp = (n: number, min: number, max: number) => Math.min(Math.max(Math.trunc(n), min), max);

/**
 * Bring a saved layout in line with the cards that exist now: cards of new instances are
 * added at the bottom (left to right, at their `default_size`), and cards of deleted
 * instances or card types are dropped.
 *
 * `knownInstances` are the instances that exist; `loadedInstances` the ones whose card
 * list was fetched. A saved card is dropped only when its instance is gone, or its card
 * list loaded and no longer has the card type, so a failed request never prunes a layout.
 */
export function mergeLayout(
  saved: CardPlacement[],
  available: AvailableCard[],
  knownInstances: ReadonlySet<string>,
  loadedInstances: ReadonlySet<string>,
): { cards: CardPlacement[]; changed: boolean } {
  const offered = new Set(available.map((a) => cardKey({ instance_id: a.instanceId, ...a.spec })));
  const kept = saved.filter(
    (c) =>
      knownInstances.has(c.instance_id) &&
      (!loadedInstances.has(c.instance_id) || offered.has(cardKey(c))),
  );
  const have = new Set(kept.map(cardKey));
  const added: CardPlacement[] = [];
  let x = 0;
  let y = kept.reduce((bottom, c) => Math.max(bottom, c.y + c.h), 0);
  let rowHeight = 0;
  for (const { instanceId, spec } of available) {
    const placement = { instance_id: instanceId, card_type: spec.card_type };
    if (have.has(cardKey(placement))) continue;
    const w = clamp(spec.default_size[0], 1, GRID_COLUMNS);
    const h = clamp(spec.default_size[1], 1, 100);
    if (x + w > GRID_COLUMNS) {
      x = 0;
      y += rowHeight;
      rowHeight = 0;
    }
    added.push({ ...placement, x, y, w, h, settings: {} });
    x += w;
    rowHeight = Math.max(rowHeight, h);
  }
  return {
    cards: [...kept, ...added],
    changed: added.length > 0 || kept.length !== saved.length,
  };
}
