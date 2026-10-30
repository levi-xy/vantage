// Dashboard-wide cards: cards that belong to no device ("Devices healthy" and the like).
import type { ComponentType } from "react";
import type { CardPlacement, Instance } from "../api";
import { AlertsCard, DevicesHealthCard, WanStatusCard } from "./DashboardWidgets";
import type { WidgetSpec } from "./layout";
import { minSize } from "./registry";
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
}

/** Dashboard-wide cards, in the order a fresh dashboard shows them. */
export const WIDGETS: readonly Widget[] = [
  {
    card_type: "devices-health",
    title: "Devices healthy",
    default_size: [3, 2],
    component: DevicesHealthCard,
  },
  { card_type: "alerts", title: "Active alerts", default_size: [3, 2], component: AlertsCard },
  { card_type: "wan-status", title: "WAN status", default_size: [3, 2], component: WanStatusCard },
  {
    card_type: "throughput",
    title: "Throughput",
    default_size: [3, 2],
    component: ThroughputCard,
    settingsEditor: ThroughputSettings,
  },
];

export function widget(cardType: string): Widget | undefined {
  return WIDGETS.find((w) => w.card_type === cardType);
}

/** The minimum size of a card: its registered minimum, or a widget's default size. */
export function cardMinSize(cardType: string, instanceId: string | null): [number, number] {
  return instanceId === null
    ? (widget(cardType)?.default_size ?? minSize(cardType))
    : minSize(cardType);
}
