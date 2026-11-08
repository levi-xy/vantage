// What a cable shows live: down when either end's device or port is down, and the rate of
// the first end whose port has counters. Pure given the live lookups.

import type { MapBox, MapCable, MapDocument, MapFrame } from "../../api";
import type { PortRef } from "../../cards/throughput/ports";
import type { MapLive } from "./mapLive";

export interface CableEnd {
  /** The device (or box label) at this end. */
  device: string;
  /** The port as it is named on the device, else as typed on the map; null for a box edge. */
  port: string | null;
  portDown: boolean;
}

export interface CableLive {
  down: boolean;
  ends: [CableEnd, CableEnd];
  /** Bits per second in/out at the first end that has counters. */
  rate: { inBps?: number; outBps?: number; ref: PortRef } | null;
}

export type EndElement = MapBox | MapFrame;

/** Boxes and frames by id: the things a cable can join. */
export function endElements(doc: MapDocument): Map<string, EndElement> {
  const out = new Map<string, EndElement>();
  for (const e of doc.elements) if (e.kind === "box" || e.kind === "frame") out.set(e.id, e);
  return out;
}

/** The device interface a cable's port id stands for (the port's `interface`, else its id). */
function ifaceOf(owner: EndElement | undefined, portId: string | null): string | null {
  if (portId === null) return null;
  const port = owner?.kind === "box" ? owner.panel?.ports.find((p) => p.id === portId) : undefined;
  return port?.interface || portId;
}

/**
 * For each box: its port ids that have a cable, with the name of the box at the cable's other
 * end (the label, else the device's name). The captions "from cables" read it, and a port
 * without link only shows red when it appears here.
 */
export function portCables(doc: MapDocument, live: MapLive): Map<string, Map<string, string>> {
  const owners = endElements(doc);
  const nameOf = (e: EndElement | undefined) =>
    e ? e.label || live.instance(e.instance_id)?.name || "" : "";
  const out = new Map<string, Map<string, string>>();
  for (const c of doc.elements) {
    if (c.kind !== "cable") continue;
    for (const [here, there] of [
      [c.from, c.to],
      [c.to, c.from],
    ] as const) {
      if (here.port === null) continue;
      const names = out.get(here.element) ?? new Map<string, string>();
      names.set(here.port, nameOf(owners.get(there.element)));
      out.set(here.element, names);
    }
  }
  return out;
}

export function cableLive(
  cable: MapCable,
  owners: ReadonlyMap<string, EndElement>,
  live: MapLive,
): CableLive {
  let rate: CableLive["rate"] = null;
  const end = (e: MapCable["from"]): CableEnd => {
    const owner = owners.get(e.element);
    const id = owner?.instance_id ?? null;
    const device = owner ? owner.label || live.instance(id)?.name || "" : "";
    const dev = live.port(id, ifaceOf(owner, e.port));
    if (dev?.ref && rate === null && (dev.inBps !== undefined || dev.outBps !== undefined)) {
      rate = { inBps: dev.inBps, outBps: dev.outBps, ref: dev.ref };
    }
    const typed =
      owner?.kind === "box" ? owner.panel?.ports.find((p) => p.id === e.port)?.caption : undefined;
    return {
      device,
      port: e.port === null ? null : typed || dev?.name || e.port,
      portDown: dev?.state === "down",
    };
  };
  const ends: [CableEnd, CableEnd] = [end(cable.from), end(cable.to)];
  const deviceDown = [cable.from, cable.to].some((e) =>
    live.isDown(owners.get(e.element)?.instance_id ?? null),
  );
  return { down: deviceDown || ends.some((e) => e.portDown), ends, rate };
}

/** The live in/out of the first cable on a box that has counters (the cloud shows it). */
export function boxRate(
  doc: MapDocument,
  boxId: string,
  live: MapLive,
): { inBps?: number; outBps?: number } | null {
  const owners = endElements(doc);
  for (const c of doc.elements) {
    if (c.kind !== "cable" || (c.from.element !== boxId && c.to.element !== boxId)) continue;
    const rate = cableLive(c, owners, live).rate;
    if (rate) return { inBps: rate.inBps, outBps: rate.outBps };
  }
  return null;
}
