import { byLabel, firstValue } from "../metrics/promql";
import { useInstantQuery } from "../metrics/useMetricQuery";

// Polled by Vantage (integrations/builtin/proxmox): vantage_proxmox_guest_*.
// The guest's name and tags are free text, so they are only on `guest_info`: join on `vmid`.
const P = "vantage_proxmox_";
type Sel = (extra?: string) => string;

/** `guest_tags` is ";"-joined, sorted and cleaned by the backend; "" or absent = none. */
function parseTags(raw: string | undefined): string[] {
  return (raw ?? "").split(";").filter((t) => t !== "");
}

export interface ProxmoxGuest {
  vmid: string;
  node: string;
  type: string;
  name: string;
  tags: string[];
  running: boolean;
}

/** One Proxmox instance's guests (running first); shared by the guests card and the network map. */
export function useProxmoxGuests(instanceId: string, { bars = true }: { bars?: boolean } = {}) {
  const running = useInstantQuery(instanceId, (sel: Sel) => `${P}guest_running${sel()}`);
  const cpu = useInstantQuery(instanceId, (sel: Sel) => `100 * ${P}guest_cpu_ratio${sel()}`, {
    enabled: bars,
  });
  const mem = useInstantQuery(
    instanceId,
    (sel: Sel) => `100 * ${P}guest_memory_used_bytes${sel()} / ${P}guest_memory_max_bytes${sel()}`,
    { enabled: bars },
  );
  const info = useInstantQuery(instanceId, (sel: Sel) => `${P}guest_info${sel()}`);
  const omitted = useInstantQuery(instanceId, (sel: Sel) => `${P}guests_omitted${sel()}`);

  const byVmid = (r: typeof cpu.data) =>
    new Map(byLabel(r, "vmid").map((x) => [x.key, x.value] as const));
  const cpuBy = byVmid(cpu.data);
  const memBy = byVmid(mem.data);
  const infoBy = new Map(byLabel(info.data, "vmid").map((x) => [x.key, x.labels]));

  const guests: ProxmoxGuest[] = byLabel(running.data, "vmid")
    .map((g) => ({
      vmid: g.key,
      node: g.labels.node ?? "",
      type: g.labels.guest_type ?? "",
      name: infoBy.get(g.key)?.guest_name || `Guest ${g.key}`,
      tags: parseTags(infoBy.get(g.key)?.guest_tags),
      running: g.value === 1,
    }))
    .sort((a, b) => Number(b.running) - Number(a.running) || Number(a.vmid) - Number(b.vmid));
  return {
    guests,
    cpuBy,
    memBy,
    omittedCount: firstValue(omitted.data) ?? 0,
    queries: { running, cpu, mem, info, omitted },
  };
}
