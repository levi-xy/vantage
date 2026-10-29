import type { CardProps } from "../registry";
import { CardFrame } from "../metrics/CardFrame";
import { formatPercent } from "../metrics/format";
import { byLabel, firstValue } from "../metrics/promql";
import { useInstantQuery } from "../metrics/useMetricQuery";

// Polled by Vantage (integrations/builtin/proxmox): vantage_proxmox_guest_*.
// The guest's name is free text, so it is only on `guest_info`: join on `vmid`.
const P = "vantage_proxmox_";
type Sel = (extra?: string) => string;

export function ProxmoxGuestsCard({ instanceId, title, instance }: CardProps) {
  const running = useInstantQuery(instanceId, (sel: Sel) => `${P}guest_running${sel()}`);
  const cpu = useInstantQuery(instanceId, (sel: Sel) => `100 * ${P}guest_cpu_ratio${sel()}`);
  const mem = useInstantQuery(
    instanceId,
    (sel: Sel) => `100 * ${P}guest_memory_used_bytes${sel()} / ${P}guest_memory_max_bytes${sel()}`,
  );
  const info = useInstantQuery(instanceId, (sel: Sel) => `${P}guest_info${sel()}`);
  const omitted = useInstantQuery(instanceId, (sel: Sel) => `${P}guests_omitted${sel()}`);

  const byVmid = (r: typeof cpu.data) =>
    new Map(byLabel(r, "vmid").map((x) => [x.key, x.value] as const));
  const cpuBy = byVmid(cpu.data);
  const memBy = byVmid(mem.data);
  const nameBy = new Map(byLabel(info.data, "vmid").map((x) => [x.key, x.labels.guest_name]));

  const guests = byLabel(running.data, "vmid")
    .map((g) => ({
      vmid: g.key,
      node: g.labels.node ?? "",
      type: g.labels.guest_type ?? "",
      name: nameBy.get(g.key) || `Guest ${g.key}`,
      running: g.value === 1,
    }))
    .sort((a, b) => Number(b.running) - Number(a.running) || Number(a.vmid) - Number(b.vmid));
  const omittedCount = firstValue(omitted.data) ?? 0;

  return (
    <CardFrame
      title={title}
      instance={instance}
      loading={running.isPending}
      error={running.error}
      empty={guests.length === 0}
      emptyHint="No guests reported yet."
    >
      <ul className="flex flex-col divide-y divide-slate-200 dark:divide-slate-800">
        {guests.map((g) => (
          <li key={g.vmid} className="flex items-center gap-2 py-1.5 text-sm">
            <span
              role="img"
              aria-label={g.running ? "running" : "stopped"}
              className={`h-2.5 w-2.5 shrink-0 rounded-full ${g.running ? "bg-emerald-500" : "bg-slate-400 dark:bg-slate-600"}`}
            />
            <span className="min-w-0 flex-1">
              <span className="block truncate font-medium">{g.name}</span>
              <span className="block text-xs text-slate-500 dark:text-slate-400">
                {g.vmid} · {g.type} · {g.node}
              </span>
            </span>
            {g.running && (
              <span className="shrink-0 text-right text-xs tabular-nums">
                <span className="block">CPU {formatPercent(cpuBy.get(g.vmid))}</span>
                <span className="block">Mem {formatPercent(memBy.get(g.vmid))}</span>
              </span>
            )}
          </li>
        ))}
      </ul>
      {omittedCount > 0 && (
        <p className="text-xs text-slate-500 dark:text-slate-400">
          {omittedCount} more guests not shown (limit set in the integration).
        </p>
      )}
    </CardFrame>
  );
}
