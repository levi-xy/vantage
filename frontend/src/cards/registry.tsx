import type { ComponentType } from "react";
import type { Instance } from "../api";
import { PlaceholderCard } from "./PlaceholderCard";

/** What every card receives. Cards fetch their data only through the typed API client. */
export interface CardProps {
  instanceId: string;
  settings: Record<string, string | number | boolean | null>;
  /** The card's title from the integration's `CardSpec`. */
  title: string;
  /** The instance, when it is loaded (always, for cards on the dashboard). */
  instance: Instance | undefined;
}

/**
 * Card components by `card_type` (the name the integration declares in its `CardSpec`).
 * Card types without an entry render `PlaceholderCard`. Register a card by adding it here.
 */
const CARD_COMPONENTS: Readonly<Record<string, ComponentType<CardProps>>> = {};

export function cardComponent(cardType: string): ComponentType<CardProps> {
  return Object.hasOwn(CARD_COMPONENTS, cardType)
    ? (CARD_COMPONENTS[cardType] as ComponentType<CardProps>)
    : PlaceholderCard;
}
