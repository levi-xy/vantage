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
import { PfsenseGatewaysCard } from "./pfsense-gateways/PfsenseGatewaysCard";
import { PfsenseInterfacesCard } from "./pfsense-interfaces/PfsenseInterfacesCard";
import { PfsenseOverviewCard } from "./pfsense-overview/PfsenseOverviewCard";
import { PlaceholderCard } from "./PlaceholderCard";
import { ProxmoxGuestsCard } from "./proxmox-guests/ProxmoxGuestsCard";
import { ProxmoxNodeCard } from "./proxmox-node/ProxmoxNodeCard";
import { SnmpOverviewCard } from "./snmp-overview/SnmpOverviewCard";
import { SnmpPortsCard } from "./snmp-ports/SnmpPortsCard";
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
  "pfsense-gateways": PfsenseGatewaysCard,
  "pfsense-interfaces": PfsenseInterfacesCard,
  "pfsense-overview": PfsenseOverviewCard,
  "ping-check": PingCheckCard,
  "proxmox-guests": ProxmoxGuestsCard,
  "proxmox-node": ProxmoxNodeCard,
  "smart-disks": SmartDisksCard,
  "snmp-overview": SnmpOverviewCard,
  "snmp-ports": SnmpPortsCard,
};

/**
 * The smallest size (grid width, height) at which each card shows everything without
 * scrolling. Cards can be made bigger, never smaller. Types not listed use DEFAULT_MIN_SIZE.
 * Dashboard-wide cards (`DashboardWidgets.tsx`) use their default size as the minimum.
 */
const MIN_SIZES: Readonly<Record<string, [number, number]>> = {
  cloudflared: [4, 3],
  "cloudflare-tunnels": [4, 3],
  "cloudflare-zones": [4, 3],
  "emby-libraries": [4, 3],
  "emby-overview": [4, 3],
  "http-check": [3, 3],
  "linux-host": [4, 4],
  "pfsense-gateways": [4, 3],
  "pfsense-interfaces": [4, 3],
  "pfsense-overview": [4, 4],
  "ping-check": [3, 3],
  "proxmox-guests": [4, 4],
  "proxmox-node": [4, 4],
  "smart-disks": [4, 3],
  "snmp-overview": [3, 3],
  "snmp-ports": [4, 4],
};
const DEFAULT_MIN_SIZE: [number, number] = [3, 2];

export function minSize(cardType: string): [number, number] {
  return Object.hasOwn(MIN_SIZES, cardType)
    ? (MIN_SIZES[cardType] as [number, number])
    : DEFAULT_MIN_SIZE;
}

export function cardComponent(cardType: string): ComponentType<CardProps> {
  return Object.hasOwn(CARD_COMPONENTS, cardType)
    ? (CARD_COMPONENTS[cardType] as ComponentType<CardProps>)
    : PlaceholderCard;
}
