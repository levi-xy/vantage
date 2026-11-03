import { useLayoutEffect, useState } from "react";
import GridLayout from "react-grid-layout";
import type { Layout } from "react-grid-layout";
import "react-grid-layout/css/styles.css";
import "react-resizable/css/styles.css";
import type { CardPlacement, Instance } from "../api";
import { cardMinSize, cardTier, widget } from "./widgets";
import { cardKey, GRID_COLUMNS, GRID_MARGIN, gridHeightPx, ROW_HEIGHT } from "./layout";
import { cardComponent, cardSettingsEditor } from "./registry";
import { CardTierContext } from "./metrics/tier";
// Used only until the container has been measured (and in test DOMs, which have no layout).
const FALLBACK_WIDTH = 1200;
const DRAG_HANDLE = "vantage-drag-handle";
const NO_DRAG = "vantage-no-drag";

/** A card's size on the grid: a card saved smaller than the minimum is shown at it. */
function shownSize(c: CardPlacement): [number, number] {
  const [minW, minH] = cardMinSize();
  return [Math.max(c.w, minW), Math.max(c.h, minH)];
}

function toGridItems(cards: CardPlacement[], editing: boolean): Layout[] {
  const [minW, minH] = cardMinSize();
  return cards.map((c) => {
    const [w, h] = shownSize(c);
    return {
      i: cardKey(c),
      x: c.x,
      y: c.y,
      w,
      h,
      minW,
      minH,
      isDraggable: editing,
      isResizable: editing,
    };
  });
}

/**
 * The width, in px, of the element the returned callback ref is put on: measured before the first paint and
 * again whenever it changes (a callback ref, so a grid shown after a phone-width list is
 * measured too). The grid is laid out to exactly this width, so it never runs
 * past its container.
 */
function useContainerWidth() {
  const [element, setElement] = useState<HTMLDivElement | null>(null);
  const [width, setWidth] = useState(0);
  useLayoutEffect(() => {
    if (!element) return;
    const measure = () => setWidth(element.clientWidth);
    measure();
    if (typeof ResizeObserver === "function") {
      const observer = new ResizeObserver(measure);
      observer.observe(element);
      return () => observer.disconnect();
    }
    window.addEventListener("resize", measure);
    return () => window.removeEventListener("resize", measure);
  }, [element]);
  return [setElement, width] as const;
}

export interface DashboardGridProps {
  cards: CardPlacement[];
  instances: ReadonlyMap<string, Instance>;
  /** Card titles by `cardKey`, from each instance's card specs. */
  titles: ReadonlyMap<string, string>;
  /**
   * Edit mode: cards can be removed, and on the desktop grid moved (by their cover) and
   * resized. The page only offers it on desktop widths.
   */
  editing: boolean;
  /** Called with the new placements after a move or resize, in edit mode. */
  onChange: (cards: CardPlacement[]) => void;
  onRemove: (card: CardPlacement) => void;
  /** Open a card's settings (cards that have a settings editor). */
  onConfigure: (card: CardPlacement) => void;
  /** Phone width: a plain list of compact cards instead of the grid (never editable). */
  compact?: boolean;
}

export function DashboardGrid({
  cards,
  instances,
  titles,
  editing,
  onChange,
  onRemove,
  onConfigure,
  compact = false,
}: DashboardGridProps) {
  const [measureRef, measuredWidth] = useContainerWidth();
  const instanceList = [...instances.values()];

  const commit = (layout: Layout[]) => {
    const byKey = new Map(layout.map((item) => [item.i, item]));
    onChange(
      cards.map((c) => {
        const item = byKey.get(cardKey(c));
        return item ? { ...c, x: item.x, y: item.y, w: item.w, h: item.h } : c;
      }),
    );
  };

  const renderCard = (c: CardPlacement, compact: boolean) => {
    const key = cardKey(c);
    const title = c.instance_id === null ? widget(c.card_type)?.title : titles.get(key);
    // Phone width: summary tiles two per row, every other card a full row.
    const tile = c.instance_id === null && widget(c.card_type)?.tile === true;
    let content;
    if (c.instance_id === null) {
      const Widget = widget(c.card_type)?.component;
      content = Widget ? <Widget instances={instanceList} settings={c.settings} /> : null;
    } else {
      const Card = cardComponent(c.card_type);
      content = (
        <Card
          instanceId={c.instance_id}
          settings={c.settings}
          title={title ?? c.card_type}
          instance={instances.get(c.instance_id)}
          compact={compact}
        />
      );
    }
    if (!compact) {
      // On the grid, a card below its full size shows less (`metrics/tier.ts`). The phone
      // list has no tier: each card is as tall as its content.
      const size = shownSize(c);
      const tier = cardTier(c.card_type, c.instance_id, size);
      content = (
        <CardTierContext.Provider value={{ tier, heightPx: gridHeightPx(size[1]) }}>
          {content}
        </CardTierContext.Provider>
      );
    }
    return (
      <div
        key={key}
        data-testid="card"
        className={`relative rounded-xl border bg-panel ${
          compact ? (tile ? "min-w-0 " : "col-span-2 ") : "overflow-hidden "
        }${editing ? "border-in" : "border-line"}`}
      >
        {content}
        {editing && (
          // Covers the card while editing: the card's own buttons don't react, and the
          // cover is what you drag. Resize with the bottom-right corner.
          <div
            className={`${DRAG_HANDLE} absolute inset-0 flex cursor-move items-center justify-center bg-white/70 backdrop-blur-[1px] dark:bg-slate-950/70`}
          >
            <span className="rounded bg-white px-2 py-1 text-sm font-medium text-slate-700 shadow-sm dark:bg-slate-800 dark:text-slate-200">
              ⠿ {title ?? c.card_type}
            </span>
            {(c.instance_id === null
              ? widget(c.card_type)?.settingsEditor
              : cardSettingsEditor(c.card_type)) && (
              <button
                type="button"
                aria-label={`Settings of ${title ?? c.card_type}`}
                onClick={() => onConfigure(c)}
                className={`${NO_DRAG} absolute top-2 right-11 rounded bg-white px-2 py-0.5 text-sm text-slate-700 shadow-sm hover:bg-sky-50 dark:bg-slate-800 dark:text-slate-200 dark:hover:bg-sky-950`}
              >
                ⚙
              </button>
            )}
            <button
              type="button"
              aria-label={`Remove ${title ?? c.card_type}`}
              onClick={() => onRemove(c)}
              className={`${NO_DRAG} absolute top-2 right-2 rounded bg-white px-2 py-0.5 text-sm text-slate-700 shadow-sm hover:bg-red-50 hover:text-red-700 dark:bg-slate-800 dark:text-slate-200 dark:hover:bg-red-950 dark:hover:text-red-300`}
            >
              ✕
            </button>
          </div>
        )}
      </div>
    );
  };

  if (compact) {
    // Phone width: reading order, each card as tall as its content; tiles two per row.
    return (
      <div className="grid grid-cols-2 gap-4">
        {[...cards].sort((a, b) => a.y - b.y || a.x - b.x).map((c) => renderCard(c, true))}
      </div>
    );
  }

  // Desktop only (phones get the compact list above), so always the 12-column grid.
  return (
    <div ref={measureRef} className="w-full min-w-0">
      <GridLayout
        className="layout"
        width={measuredWidth || FALLBACK_WIDTH}
        layout={toGridItems(cards, editing)}
        cols={GRID_COLUMNS}
        rowHeight={ROW_HEIGHT}
        margin={[GRID_MARGIN, GRID_MARGIN]}
        containerPadding={[0, 0]}
        // Cards float up into any free space: no holes below a short card.
        compactType="vertical"
        isDraggable={editing}
        isResizable={editing}
        draggableHandle={`.${DRAG_HANDLE}`}
        draggableCancel={`.${NO_DRAG}`}
        onDragStop={commit}
        onResizeStop={commit}
      >
        {cards.map((c) => renderCard(c, false))}
      </GridLayout>
    </div>
  );
}
