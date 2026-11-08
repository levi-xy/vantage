// Live data for one map: the instance list (status, kept fresh by /ws and a slow refetch),
// Prometheus' scrape state for devices the collector doesn't poll, and one shared fleet query
// per figure in use. Nothing here polls per element.

import type { Instance, InstanceStatus, MapDocument } from "../../api";
import { shownState, useScrapeStates } from "../../cards/metrics/scrapeStatus";
import { useInstances, useIntegrations } from "../devices/queries";
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
  /** The figures of a linked box with their current values, in display order. */
  figures: (box: { figures: string[] }, instance: Instance) => { def: FigureDef; text: string }[];
}

export function useMapLive(doc: MapDocument | undefined): MapLive {
  const instances = useInstances();
  const integrations = useIntegrations();
  const list = instances.data ?? [];
  const byId = new Map(list.map((i) => [i.id, i] as const));
  const scrape = useScrapeStates(list);

  // Only the figures of the integrations actually linked on this map.
  const wanted = new Set<FigureDef>();
  for (const el of doc?.elements ?? []) {
    if (el.kind !== "box" || !el.show.figures) continue;
    const inst = el.instance_id ? byId.get(el.instance_id) : undefined;
    if (inst) for (const def of boxFigures(el, inst.type_id)) wanted.add(def);
  }
  const values = useFigureValues([...wanted]);

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
