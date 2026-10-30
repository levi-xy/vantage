import { useState } from "react";
import { Responsive, WidthProvider } from "react-grid-layout";
import type { Layout } from "react-grid-layout";
import "react-grid-layout/css/styles.css";
import "react-resizable/css/styles.css";
import type { CardPlacement, Instance } from "../api";
import { cardMinSize, widget } from "./widgets";
import { cardKey, DESKTOP_MIN_WIDTH, GRID_COLUMNS } from "./layout";
import { cardComponent } from "./registry";

const ResponsiveGrid = WidthProvider(Responsive);

// Below 768 px the grid is a single column, and never editable: editing is for desktop.
const BREAKPOINTS = { lg: DESKTOP_MIN_WIDTH, xs: 0 };
const COLS = { lg: GRID_COLUMNS, xs: 1 };
const ROW_HEIGHT = 56;
const DRAG_HANDLE = "vantage-drag-handle";
const NO_DRAG = "vantage-no-drag";

function toGridItems(cards: CardPlacement[], editing: boolean): Layout[] {
  return cards.map((c) => {
    const [minW, minH] = cardMinSize(c.card_type, c.instance_id);
    return {
      i: cardKey(c),
      x: c.x,
      y: c.y,
      // A card saved smaller than its minimum (before minimums existed) is shown at it.
      w: Math.max(c.w, minW),
      h: Math.max(c.h, minH),
      minW,
      minH,
      isDraggable: editing,
      isResizable: editing,
    };
  });
}

/** One column in reading order (top to bottom, then left to right). */
function toColumn(cards: CardPlacement[]): Layout[] {
  let y = 0;
  return [...cards]
    .sort((a, b) => a.y - b.y || a.x - b.x)
    .map((c) => {
      const h = Math.max(c.h, cardMinSize(c.card_type, c.instance_id)[1]);
      const item = { i: cardKey(c), x: 0, y, w: 1, h, isDraggable: false, isResizable: false };
      y += h;
      return item;
    });
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
}

export function DashboardGrid({
  cards,
  instances,
  titles,
  editing,
  onChange,
  onRemove,
}: DashboardGridProps) {
  const [breakpoint, setBreakpoint] = useState("lg");
  const canEdit = editing && breakpoint === "lg";
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

  return (
    <ResponsiveGrid
      className="layout"
      layouts={{ lg: toGridItems(cards, canEdit), xs: toColumn(cards) }}
      breakpoints={BREAKPOINTS}
      cols={COLS}
      rowHeight={ROW_HEIGHT}
      margin={[16, 16]}
      containerPadding={[0, 0]}
      isDraggable={canEdit}
      isResizable={canEdit}
      draggableHandle={`.${DRAG_HANDLE}`}
      draggableCancel={`.${NO_DRAG}`}
      onBreakpointChange={setBreakpoint}
      onDragStop={commit}
      onResizeStop={commit}
    >
      {cards.map((c) => {
        const key = cardKey(c);
        const title = c.instance_id === null ? widget(c.card_type)?.title : titles.get(key);
        let content;
        if (c.instance_id === null) {
          const Widget = widget(c.card_type)?.component;
          content = Widget ? <Widget instances={instanceList} /> : null;
        } else {
          const Card = cardComponent(c.card_type);
          content = (
            <Card
              instanceId={c.instance_id}
              settings={c.settings}
              title={title ?? c.card_type}
              instance={instances.get(c.instance_id)}
            />
          );
        }
        return (
          <div
            key={key}
            data-testid="card"
            className={`relative overflow-hidden rounded-lg border bg-white dark:bg-slate-900 ${
              editing
                ? "border-blue-400 dark:border-blue-500"
                : "border-slate-200 dark:border-slate-800"
            }`}
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
      })}
    </ResponsiveGrid>
  );
}
