import type { ComponentType } from "react";
import type { Instance } from "../api";
import { CloudflareTunnelsCard } from "./cloudflare-tunnels/CloudflareTunnelsCard";
import { CloudflareZonesCard } from "./cloudflare-zones/CloudflareZonesCard";
import { CloudflaredCard } from "./cloudflared/CloudflaredCard";
import { EmbyLibrariesCard } from "./emby-libraries/EmbyLibrariesCard";
import { EmbyOverviewCard } from "./emby-overview/EmbyOverviewCard";
import { HttpCheckCard } from "./http-check/HttpCheckCard";
import { LinuxHostCard } from "./linux-host/LinuxHostCard";
import { PingCheckCard } from "./ping-check/PingCheckCard";
import { PlaceholderCard } from "./PlaceholderCard";
import { SmartDisksCard } from "./smart-disks/SmartDisksCard";

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
const CARD_COMPONENTS: Readonly<Record<string, ComponentType<CardProps>>> = {
  cloudflared: CloudflaredCard,
  "cloudflare-tunnels": CloudflareTunnelsCard,
  "cloudflare-zones": CloudflareZonesCard,
  "emby-libraries": EmbyLibrariesCard,
  "emby-overview": EmbyOverviewCard,
  "http-check": HttpCheckCard,
  "linux-host": LinuxHostCard,
  "ping-check": PingCheckCard,
  "smart-disks": SmartDisksCard,
};

export function cardComponent(cardType: string): ComponentType<CardProps> {
  return Object.hasOwn(CARD_COMPONENTS, cardType)
    ? (CARD_COMPONENTS[cardType] as ComponentType<CardProps>)
    : PlaceholderCard;
}
