// What a box says about itself: the identity lines (model, address, version) and the items
// of its sections that have something to show. Pure given the live lookups.

import { colorVar, isDeviceColor } from "../../app/deviceColors";
import type { Instance, MapBox, MapElement, MapFrame, MapItem, MapSection } from "../../api";
import { instanceAddress } from "./mapLive";
import type { MapLive } from "./mapLive";

export interface Identity {
  model: string;
  address: string;
  version: string;
}

/**
 * The lines a box shows: what the admin typed, else what Vantage knows of the linked device
 * (the integration's name, the configured host), else nothing (the line is hidden).
 */
export function boxIdentity(
  box: Pick<MapBox, "model" | "address" | "version">,
  instance: Instance | undefined,
  live: Pick<MapLive, "typeName">,
): Identity {
  return {
    model: box.model || (instance ? live.typeName(instance.type_id) : ""),
    address: box.address || (instance ? instanceAddress(instance) : ""),
    version: box.version,
  };
}

export interface ShownItem {
  item: MapItem;
  /** The text of the value: the live figure, else the typed value. */
  value: string;
  /** True when `value` is a live figure from the device (shown with a green dot). */
  live: boolean;
}

/**
 * The items of a section that are shown. An item with a `figure` needs live data for it and
 * is hidden without (never a "–"); other items show their typed value.
 */
export function shownItems(
  section: MapSection,
  instanceId: string | null,
  live: Pick<MapLive, "figure">,
): ShownItem[] {
  const out: ShownItem[] = [];
  for (const item of section.items) {
    if (item.figure) {
      const value = live.figure(instanceId, item.figure);
      if (value !== undefined) out.push({ item, value, live: true });
    } else {
      out.push({ item, value: item.value, live: false });
    }
  }
  return out;
}

export const paint = (name: string | null | undefined): string | undefined =>
  isDeviceColor(name) ? colorVar(name) : undefined;

/** A linked box or frame opens the details drawer: a button for mouse, touch and keyboard. */
export function selectable(
  id: string,
  linked: boolean,
  onSelect: ((id: string) => void) | undefined,
  label: string,
): Record<string, unknown> {
  if (!linked || !onSelect) return {};
  return {
    role: "button",
    tabIndex: 0,
    "aria-label": `Details of ${label}`,
    onClick: () => onSelect(id),
    onKeyDown: (e: { key: string; preventDefault: () => void }) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        onSelect(id);
      }
    },
  };
}

/** The height a new box gets in the editor: not a chosen height, so content is never scaled to it. */
export const DEFAULT_BOX_H = 88;

/**
 * Elements whose height follows the content (only their width is stored): the networks and
 * legend panels, filled frames and clouds. A box can be made taller than its content, which
 * then scales up to fill it.
 */
export const contentSized = (e: MapElement): boolean =>
  (e.kind === "box" && e.shape === "cloud") ||
  e.kind === "networks" ||
  e.kind === "legend" ||
  (e.kind === "frame" && frameSized(e));

/**
 * A frame with something to show is as tall as its content; an empty one keeps its stored
 * height so it can still be drawn around other things.
 */
export const frameSized = (f: Pick<MapFrame, "instance_id" | "guests" | "side">): boolean =>
  f.instance_id !== null || f.guests.manual.length > 0 || f.side !== null;
