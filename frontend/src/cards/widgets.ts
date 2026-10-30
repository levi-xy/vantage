// Dashboard-wide cards: cards that belong to no device ("Devices healthy" and the like).
import type { ComponentType } from "react";
import type { Instance } from "../api";
import { AlertsCard, DevicesHealthCard, ThroughputCard, WanStatusCard } from "./DashboardWidgets";
import type { WidgetSpec } from "./layout";
import { minSize } from "./registry";

/** What a dashboard-wide card receives: every instance, not one. */
export interface WidgetProps {
  instances: Instance[];
}

export interface Widget extends WidgetSpec {
  component: ComponentType<WidgetProps>;
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
    title: "Total throughput",
    default_size: [3, 2],
    component: ThroughputCard,
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
