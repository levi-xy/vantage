import type { ComponentType } from "react";
import type { Instance } from "../api";
import { HttpCheckCard } from "./http-check/HttpCheckCard";
import { LinuxHostCard } from "./linux-host/LinuxHostCard";
import { PingCheckCard } from "./ping-check/PingCheckCard";
import { PfsenseGatewaysCard } from "./pfsense-gateways/PfsenseGatewaysCard";
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
  "http-check": HttpCheckCard,
  "linux-host": LinuxHostCard,
  "pfsense-gateways": PfsenseGatewaysCard,
  "pfsense-overview": PfsenseOverviewCard,
  "ping-check": PingCheckCard,
  "proxmox-guests": ProxmoxGuestsCard,
  "proxmox-node": ProxmoxNodeCard,
  "smart-disks": SmartDisksCard,
  "snmp-overview": SnmpOverviewCard,
  "snmp-ports": SnmpPortsCard,
};

export function cardComponent(cardType: string): ComponentType<CardProps> {
  return Object.hasOwn(CARD_COMPONENTS, cardType)
    ? (CARD_COMPONENTS[cardType] as ComponentType<CardProps>)
    : PlaceholderCard;
}
