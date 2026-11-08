import type { MapGuests } from "../../api";
import type { ProxmoxGuest } from "../../cards/proxmox-guests/useProxmoxGuests";

export interface GuestTile {
  key: string;
  name: string;
  /** Running, stopped, or null for a typed-in tile (no live state). */
  running: boolean | null;
  subtitle: string;
}

const KIND: Record<string, string> = { qemu: "VM", lxc: "CT" };

/**
 * A frame's tiles: the instance's guests (unless `auto` is off) without the hidden ones,
 * those named in `order` first in that order, then the rest as they come, then the typed-in
 * tiles. `hidden` and `order` hold the guest's vmid.
 */
export function guestTiles(guests: ProxmoxGuest[], config: MapGuests): GuestTile[] {
  const shown = config.auto ? guests.filter((g) => !config.hidden.includes(g.vmid)) : [];
  const ordered = orderGuests(shown, config.order);
  return [
    ...ordered.map((g) => ({
      key: `g${g.vmid}`,
      name: g.name,
      running: g.running,
      subtitle: [`${KIND[g.type] ?? g.type} ${g.vmid}`, g.node].filter(Boolean).join(" · "),
    })),
    ...config.manual.map((m) => ({
      key: `m${m.id}`,
      name: m.label,
      running: null,
      subtitle: m.subtitle,
    })),
  ];
}

/** Guests with those named in `order` first, in that order, then the rest as they come. */
export function orderGuests(guests: ProxmoxGuest[], order: readonly string[]): ProxmoxGuest[] {
  const rank = (g: ProxmoxGuest) => {
    const i = order.indexOf(g.vmid);
    return i < 0 ? Infinity : i;
  };
  return [...guests].sort((a, b) => {
    const [ra, rb] = [rank(a), rank(b)];
    return ra === rb ? 0 : ra < rb ? -1 : 1;
  });
}
