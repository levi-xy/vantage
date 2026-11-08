import type { MapFrame, MapGuests } from "../../api";
import type { ProxmoxGuest } from "../../cards/proxmox-guests/useProxmoxGuests";

export interface GuestTile {
  key: string;
  name: string;
  /** Running, stopped, or null for a typed-in tile (no live state). */
  running: boolean | null;
  subtitle: string;
  /** "CT 109": the guest's kind and id (empty for a typed-in tile). */
  ref: string;
  /** Where the live CPU / memory of a Proxmox guest are keyed (empty for a typed-in tile). */
  vmid: string;
  /** The node it runs on (empty for a typed-in tile). */
  node: string;
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
      ref: `${KIND[g.type] ?? g.type} ${g.vmid}`,
      vmid: g.vmid,
      node: g.node,
    })),
    ...config.manual.map((m) => ({
      key: `m${m.id}`,
      name: m.label,
      running: null,
      subtitle: m.subtitle,
      ref: "",
      vmid: "",
      node: "",
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

export interface GuestColumn {
  id: string;
  /** Empty for the single untitled column of a frame without groups. */
  title: string;
  tiles: GuestTile[];
}

/**
 * A frame's tiles split into its groups: each tile goes to the first group that lists its key;
 * tiles in no group go to a trailing "Other" column. Columns without tiles are left out, so
 * nothing is drawn empty. A frame without groups is one untitled column.
 */
export function guestColumns(tiles: GuestTile[], groups: MapFrame["groups"]): GuestColumn[] {
  if (groups.length === 0) return tiles.length > 0 ? [{ id: "", title: "", tiles }] : [];
  const placed = new Set<string>();
  const columns: GuestColumn[] = groups.map((g) => {
    const mine = tiles.filter((t) => g.members.includes(t.key) && !placed.has(t.key));
    for (const t of mine) placed.add(t.key);
    return { id: g.id, title: g.title, tiles: mine };
  });
  const other = tiles.filter((t) => !placed.has(t.key));
  if (other.length > 0) columns.push({ id: "other", title: "Other", tiles: other });
  return columns.filter((c) => c.tiles.length > 0);
}
