import { useState } from "react";
import { Responsive, WidthProvider } from "react-grid-layout";
import type { Layout } from "react-grid-layout";
import "react-grid-layout/css/styles.css";
import "react-resizable/css/styles.css";
import type { CardPlacement, Instance } from "../api";
import { cardKey, GRID_COLUMNS } from "./layout";
import { cardComponent } from "./registry";

const ResponsiveGrid = WidthProvider(Responsive);

// Below 768 px the grid is a single column (and not editable: drag-and-drop is for desktop).
const BREAKPOINTS = { lg: 768, xs: 0 };
const COLS = { lg: GRID_COLUMNS, xs: 1 };
const ROW_HEIGHT = 56;

function toGridItems(cards: CardPlacement[]): Layout[] {
  return cards.map((c) => ({ i: cardKey(c), x: c.x, y: c.y, w: c.w, h: c.h, minW: 1, minH: 1 }));
}

/** One column in reading order (top to bottom, then left to right). */
function toColumn(cards: CardPlacement[]): Layout[] {
  let y = 0;
  return [...cards]
    .sort((a, b) => a.y - b.y || a.x - b.x)
    .map((c) => {
      const item = { i: cardKey(c), x: 0, y, w: 1, h: c.h };
      y += c.h;
      return item;
    });
}

export interface DashboardGridProps {
  cards: CardPlacement[];
  instances: ReadonlyMap<string, Instance>;
  /** Card titles by `cardKey`, from each instance's card specs. */
  titles: ReadonlyMap<string, string>;
  /** Admins can drag and resize (on desktop). */
  editable: boolean;
  /** Called with the new placements after a drag or resize ends. */
  onChange: (cards: CardPlacement[]) => void;
}

export function DashboardGrid({
  cards,
  instances,
  titles,
  editable,
  onChange,
}: DashboardGridProps) {
  const [breakpoint, setBreakpoint] = useState("lg");
  const canEdit = editable && breakpoint === "lg";

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
      layouts={{ lg: toGridItems(cards), xs: toColumn(cards) }}
      breakpoints={BREAKPOINTS}
      cols={COLS}
      rowHeight={ROW_HEIGHT}
      margin={[16, 16]}
      isDraggable={canEdit}
      isResizable={canEdit}
      onBreakpointChange={setBreakpoint}
      onDragStop={commit}
      onResizeStop={commit}
    >
      {cards.map((c) => {
        const Card = cardComponent(c.card_type);
        return (
          <div
            key={cardKey(c)}
            data-testid="card"
            className="overflow-hidden rounded-lg border border-slate-200 bg-white dark:border-slate-800 dark:bg-slate-900"
          >
            <Card
              instanceId={c.instance_id}
              settings={c.settings}
              title={titles.get(cardKey(c)) ?? c.card_type}
              instance={instances.get(c.instance_id)}
            />
          </div>
        );
      })}
    </ResponsiveGrid>
  );
}
