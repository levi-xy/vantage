// Live data for one map: the instance list (status, kept fresh by /ws and a slow refetch),
// Prometheus' scrape state for devices the collector doesn't poll, and one shared fleet query
// per figure in use. Nothing here polls per element.

import type { Instance, InstanceStatus, MapDocument } from "../../api";
import { shownState, useScrapeStates } from "../../cards/metrics/scrapeStatus";
import { useInstances, useIntegrations } from "../devices/queries";
import { hasPorts, useDevicePorts } from "./devicePorts";
import type { DevicePort } from "./devicePorts";
import { boxFigures, useFigureValues } from "./figures";
import type { FigureDef } from "./figures";

export type DeviceState = InstanceStatus["state"] | "removed";

export interface MapLive {
  /** False until the instance list has loaded (nothing is "removed" before that). */
  ready: boolean;
  instance: (id: string | null) => Instance | undefined;
  /** `removed`: the id names no instance. */
  state: (id: string | null) => DeviceState | null;
  isDown: (id: string | null) => boolean;
  typeName: (typeId: string) => string;
  /** A linked device's own interfaces (undefined: unknown device or not loaded yet). */
  ports: (id: string | null) => DevicePort[] | undefined;
  port: (id: string | null, portId: string | null) => DevicePort | undefined;
  /** The figures of a linked box with their current values, in display order. */
  figures: (box: { figures: string[] }, instance: Instance) => { def: FigureDef; text: string }[];
}

export function useMapLive(doc: MapDocument | undefined, drawing = true): MapLive {
  const instances = useInstances();
  const integrations = useIntegrations();
  const list = instances.data ?? [];
  const byId = new Map(list.map((i) => [i.id, i] as const));
  const scrape = useScrapeStates(list);

  // A phone shows the list only: no figure or interface queries then.
  const elements = drawing ? (doc?.elements ?? []) : [];

  // Only the figures of the integrations actually linked on this map (defaults included).
  const wanted = new Set<FigureDef>();
  for (const el of elements) {
    // Frames and boxes without figures are included: the details drawer shows them too.
    if (el.kind !== "box" && el.kind !== "frame") continue;
    const inst = el.instance_id ? byId.get(el.instance_id) : undefined;
    const named = el.kind === "box" ? el : { figures: [] };
    if (inst) for (const def of boxFigures(named, inst.type_id)) wanted.add(def);
  }
  const values = useFigureValues([...wanted]);

  // Interfaces are read for devices with a port strip or a cable that names a port.
  const needPorts = new Set<string>();
  for (const el of elements) {
    if (el.kind === "box" && el.panel && el.instance_id) needPorts.add(el.instance_id);
    if (el.kind !== "cable") continue;
    for (const end of [el.from, el.to]) {
      const owner = elements.find((e) => e.id === end.element);
      if (end.port && owner && "instance_id" in owner && owner.instance_id) {
        needPorts.add(owner.instance_id);
      }
    }
  }
  const devicePorts = useDevicePorts(list.filter((i) => needPorts.has(i.id) && hasPorts(i)));

  const ready = instances.isSuccess;
  const instance = (id: string | null) => (id ? byId.get(id) : undefined);
  const state = (id: string | null): DeviceState | null => {
    if (!id) return null;
    const inst = byId.get(id);
    if (!inst) return ready ? "removed" : null;
    return shownState(inst, scrape);
  };
  return {
    ready,
    instance,
    state,
    isDown: (id) => state(id) === "down",
    typeName: (typeId) => integrations.data?.find((t) => t.type_id === typeId)?.name ?? typeId,
    ports: (id) => (id ? devicePorts.get(id) : undefined),
    port: (id, portId) =>
      id && portId ? devicePorts.get(id)?.find((p) => p.id === portId) : undefined,
    figures: (box, inst) =>
      boxFigures(box, inst.type_id).map((def) => ({
        def,
        text: def.format(values.get(def)?.get(inst.id)),
      })),
  };
}

/** `host` or `address` from a device's non-secret config, for the "type · address" line. */
export function instanceAddress(instance: Instance): string {
  for (const key of ["host", "address", "hostname"]) {
    const v = instance.config[key];
    if (typeof v === "string" && v) return v;
  }
  const url = instance.config.url;
  if (typeof url === "string") {
    try {
      return new URL(url).host;
    } catch {
      return "";
    }
  }
  return "";
}
