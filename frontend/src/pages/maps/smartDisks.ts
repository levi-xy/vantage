// The disks a SMART exporter reports, for the drive bays of a NAS box. The same metrics the
// SMART disks card reads, asked once for the whole fleet (never one query per box) and
// grouped per instance, like the other map figures.

import { useQueries } from "@tanstack/react-query";
import type { MapNas, QueryResult } from "../../api";
import { useApiClient } from "../../app/session";
import { INSTANT_REFETCH_MS } from "../../cards/metrics/useMetricQuery";
import { parseValue } from "../../cards/metrics/promql";
import { useDocumentHidden } from "./useDocumentHidden";

export interface SmartDisk {
  /** The device name the exporter uses (`sda`). */
  device: string;
  serial: string;
  model: string;
  /** True when the overall SMART health passed; undefined when not reported. */
  healthy: boolean | undefined;
  /** Celsius. */
  temperature: number | undefined;
  /** Bytes. */
  size: number | undefined;
}

export type SmartDisks = ReadonlyMap<string, SmartDisk[]>;

/** At this temperature a disk's LED turns amber. */
export const WARM_CELSIUS = 45;

// Fixed query strings; native smartctl_exporter names, the four standard labels on every series.
const QUERIES = [
  ["info", "smartctl_device"],
  ["status", "smartctl_device_smart_status"],
  ["temperature", 'smartctl_device_temperature{temperature_type="current"}'],
  ["size", "smartctl_device_capacity_bytes"],
] as const;

const keyOf = (instance: string, device: string) => `${instance}\u0000${device}`;

function rows(result: QueryResult | undefined) {
  return (result?.series ?? []).flatMap((s) => {
    const instance = s.labels.vantage_instance_id;
    const device = s.labels.device;
    return instance && device
      ? [{ instance, device, labels: s.labels, value: parseValue(s.values[0]?.[1]) }]
      : [];
  });
}

function build(results: Map<string, QueryResult | undefined>): SmartDisks {
  const part = (name: string) =>
    new Map(rows(results.get(name)).map((r) => [keyOf(r.instance, r.device), r.value] as const));
  const status = part("status");
  const temperature = part("temperature");
  const size = part("size");
  const out = new Map<string, SmartDisk[]>();
  for (const r of rows(results.get("info"))) {
    const k = keyOf(r.instance, r.device);
    const flag = status.get(k);
    const disk: SmartDisk = {
      device: r.device,
      serial: r.labels.serial_number ?? "",
      model: r.labels.model_name ?? "",
      healthy: flag === undefined ? undefined : flag === 1,
      temperature: temperature.get(k),
      size: size.get(k),
    };
    out.set(r.instance, [...(out.get(r.instance) ?? []), disk]);
  }
  for (const list of out.values()) list.sort((a, b) => a.device.localeCompare(b.device));
  return out;
}

/** The disks of every instance, asked only while `enabled` (a NAS with bays is on the map). */
export function useSmartDisks(enabled: boolean): SmartDisks {
  const client = useApiClient();
  const hidden = useDocumentHidden();
  return useQueries({
    queries: QUERIES.map(([, expr]) => ({
      queryKey: ["query", "fleet", expr],
      queryFn: () => client.query({ query: expr }),
      enabled,
      refetchInterval: hidden ? false : INSTANT_REFETCH_MS,
    })),
    combine: (res) => build(new Map(QUERIES.map(([name], i) => [name, res[i]?.data] as const))),
  });
}

/** The disk a bay's `disk` text names: by device name or serial number, ignoring case. */
export function matchDisk(disks: readonly SmartDisk[], disk: string): SmartDisk | undefined {
  const want = disk.trim().toLowerCase();
  if (!want) return undefined;
  return disks.find((d) => d.device.toLowerCase() === want || d.serial.toLowerCase() === want);
}

export type BayState = "healthy" | "warm" | "failing" | "unknown" | "empty";

/** A bay's LED: failing health first, then warm, healthy, else unknown (a disk but no data). */
export function bayState(occupied: boolean, disk: SmartDisk | undefined): BayState {
  if (!occupied) return "empty";
  if (!disk) return "unknown";
  if (disk.healthy === false) return "failing";
  if ((disk.temperature ?? 0) >= WARM_CELSIUS) return "warm";
  return disk.healthy ? "healthy" : "unknown";
}

export interface Bay {
  n: number;
  label: string;
  state: BayState;
  disk: SmartDisk | undefined;
}

/** The bays of a NAS, in order, with their state from the matched SMART disks. */
export function bays(nas: MapNas, disks: readonly SmartDisk[]): Bay[] {
  return Array.from({ length: nas.bays }, (_, i) => {
    const entry = nas.bay_disks.find((b) => b.bay === i + 1);
    const occupied = entry !== undefined && (entry.disk !== "" || entry.label !== "");
    const disk = entry ? matchDisk(disks, entry.disk) : undefined;
    return {
      n: i + 1,
      label: entry?.label || String(i + 1),
      state: bayState(occupied, disk),
      disk,
    };
  });
}
