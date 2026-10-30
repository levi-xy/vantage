// Pure helpers for the network map card: node positions and matching a link's port name to
// a port that has traffic counters.
import type { DeviceLink, Instance } from "../../api";
import type { PortOption, PortRef } from "../throughput/ports";

export const MAP_WIDTH = 600;
export const MAP_HEIGHT = 360;

export interface Point {
  x: number;
  y: number;
}

/** Instances on an ellipse, sorted by name, starting at the top. One alone sits in the middle. */
export function circleLayout(instances: Instance[]): Map<string, Point> {
  const sorted = [...instances].sort((a, b) => a.name.localeCompare(b.name));
  const cx = MAP_WIDTH / 2;
  const cy = MAP_HEIGHT / 2 - 10;
  const rx = MAP_WIDTH / 2 - 70;
  const ry = MAP_HEIGHT / 2 - 50;
  const out = new Map<string, Point>();
  sorted.forEach((inst, i) => {
    if (sorted.length === 1) return out.set(inst.id, { x: cx, y: cy });
    const angle = -Math.PI / 2 + (2 * Math.PI * i) / sorted.length;
    out.set(inst.id, { x: cx + rx * Math.cos(angle), y: cy + ry * Math.sin(angle) });
  });
  return out;
}

/** The port of `instanceId` that `name` (an interface name or its shown name) refers to. */
export function findPort(
  groups: { instance: Instance; ports: PortOption[] }[],
  instanceId: string,
  name: string | null,
): PortRef | null {
  if (!name) return null;
  const wanted = name.trim().toLowerCase();
  const group = groups.find((g) => g.instance.id === instanceId);
  const match = group?.ports.find(
    (o) => o.port.iface.toLowerCase() === wanted || o.name.toLowerCase() === wanted,
  );
  return match?.port ?? null;
}

/** Links whose two devices are both in `instances` (a link to a deleted device is not drawn). */
export function drawableLinks(links: DeviceLink[], instances: Instance[]): DeviceLink[] {
  const ids = new Set(instances.map((i) => i.id));
  return links.filter((l) => ids.has(l.a_instance_id) && ids.has(l.b_instance_id));
}
