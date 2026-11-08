// Dashboard-wide cards: cards that belong to no device ("Devices healthy" and the like).
import type { ComponentType } from "react";
import type { CardPlacement, Instance } from "../api";
import { AlertsCard, DevicesHealthCard, WanStatusCard } from "./DashboardWidgets";
import type { WidgetSpec } from "./layout";
import { fullSize } from "./registry";
import { SMALLEST_SIZE, tierFor } from "./metrics/tier";
import type { CardTier } from "./metrics/tier";
import { ThroughputCard, ThroughputSettings } from "./throughput/ThroughputCard";

type Settings = CardPlacement["settings"];

/** What a dashboard-wide card receives: every instance, not one, and its settings. */
export interface WidgetProps {
  instances: Instance[];
  settings: Settings;
}

/** A card's settings editor, shown in edit mode (the ⚙ button on the card). */
export interface WidgetSettingsProps extends WidgetProps {
  onChange: (settings: Settings) => void;
}

export interface Widget extends WidgetSpec {
  component: ComponentType<WidgetProps>;
  settingsEditor?: ComponentType<WidgetSettingsProps>;
  /** A small summary tile: two per row at phone width instead of one. */
  tile?: boolean;
  /** From this size on the card shows everything; default: its default size. */
  fullSize?: [number, number];
}

/** Dashboard-wide cards, in the order a fresh dashboard shows them. */
export const WIDGETS: readonly Widget[] = [
  {
    card_type: "devices-health",
    title: "Devices healthy",
    default_size: [3, 2],
    component: DevicesHealthCard,
    tile: true,
    fullSize: [2, 2],
  },
  {
    card_type: "alerts",
    title: "Active alerts",
    default_size: [3, 2],
    component: AlertsCard,
    tile: true,
    fullSize: [2, 2],
  },
  {
    card_type: "wan-status",
    title: "WAN status",
    default_size: [3, 2],
    component: WanStatusCard,
    tile: true,
    fullSize: [2, 2],
  },
  {
    card_type: "throughput",
    title: "Throughput",
    // Room for the 6-hour chart; a full row at phone width.
    default_size: [6, 5],
    component: ThroughputCard,
    settingsEditor: ThroughputSettings,
  },
];

export function widget(cardType: string): Widget | undefined {
  return WIDGETS.find((w) => w.card_type === cardType);
}

/** The minimum size of every card: 2 x 1. Below its full size a card shows less. */
export function cardMinSize(): [number, number] {
  return SMALLEST_SIZE;
}

/** The size from which a card shows everything (its full tier). */
export function cardFullSize(cardType: string, instanceId: string | null): [number, number] {
  if (instanceId === null) {
    const w = widget(cardType);
    if (w) return w.fullSize ?? w.default_size;
  }
  return fullSize(cardType);
}

/** How much a card of this size shows. */
export function cardTier(
  cardType: string,
  instanceId: string | null,
  size: [number, number],
): CardTier {
  return tierFor(size, cardFullSize(cardType, instanceId));
}
