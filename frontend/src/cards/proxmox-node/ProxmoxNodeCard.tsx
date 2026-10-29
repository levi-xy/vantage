import type { CardProps } from "../registry";
import { CardFrame, UsageBar } from "../metrics/CardFrame";
import { formatDuration, formatPercent } from "../metrics/format";
import { byLabel } from "../metrics/promql";
import { useInstantQuery } from "../metrics/useMetricQuery";

// Polled by Vantage (integrations/builtin/proxmox): vantage_proxmox_*, label `node`.
const P = "vantage_proxmox_";
type Sel = (extra?: string) => string;

export function ProxmoxNodeCard({ instanceId, title, instance }: CardProps) {
  const up = useInstantQuery(instanceId, (sel: Sel) => `${P}node_up${sel()}`);
  const cpu = useInstantQuery(instanceId, (sel: Sel) => `100 * ${P}node_cpu_ratio${sel()}`);
  const mem = useInstantQuery(
    instanceId,
    (sel: Sel) => `100 * ${P}node_memory_used_bytes${sel()} / ${P}node_memory_total_bytes${sel()}`,
  );
  const root = useInstantQuery(
    instanceId,
    (sel: Sel) =>
      `100 * ${P}node_root_disk_used_bytes${sel()} / ${P}node_root_disk_total_bytes${sel()}`,
  );
  const uptime = useInstantQuery(instanceId, (sel: Sel) => `${P}node_uptime_seconds${sel()}`);
  const storage = useInstantQuery(
    instanceId,
    (sel: Sel) => `100 * ${P}storage_used_bytes${sel()} / ${P}storage_total_bytes${sel()}`,
  );

  const pick = (r: typeof cpu.data) =>
    new Map(byLabel(r, "node").map((x) => [x.key, x.value] as const));
  const cpuBy = pick(cpu.data);
  const memBy = pick(mem.data);
  const rootBy = pick(root.data);
  const uptimeBy = pick(uptime.data);
  const nodes = byLabel(up.data, "node").sort((a, b) => a.key.localeCompare(b.key));
  const storages = byLabel(storage.data, "storage")
    .map((s) => ({ node: s.labels.node ?? "", name: s.key, value: s.value }))
    .filter((s) => s.value !== undefined)
    .sort((a, b) => `${a.node}/${a.name}`.localeCompare(`${b.node}/${b.name}`));
  const multiNode = nodes.length > 1;

  return (
    <CardFrame
      title={title}
      instance={instance}
      loading={up.isPending}
      error={up.error}
      empty={nodes.length === 0}
    >
      {nodes.map((n) => {
        const online = n.value === 1;
        return (
          <section key={n.key} aria-label={`Node ${n.key}`} className="flex flex-col gap-1.5">
            <div className="flex items-baseline justify-between gap-2">
              <span className="font-semibold">{n.key}</span>
              <span
                className={`text-xs font-medium ${online ? "text-emerald-700 dark:text-emerald-400" : "text-red-700 dark:text-red-400"}`}
              >
                {online ? `online · up ${formatDuration(uptimeBy.get(n.key))}` : "offline"}
              </span>
            </div>
            {online && (
              <>
                <UsageBar
                  label="CPU"
                  percent={cpuBy.get(n.key) ?? 0}
                  text={formatPercent(cpuBy.get(n.key))}
                />
                <UsageBar
                  label="Memory"
                  percent={memBy.get(n.key) ?? 0}
                  text={formatPercent(memBy.get(n.key))}
                />
                <UsageBar
                  label="Root disk"
                  percent={rootBy.get(n.key) ?? 0}
                  text={formatPercent(rootBy.get(n.key))}
                />
              </>
            )}
          </section>
        );
      })}
      {storages.length > 0 && (
        <section aria-label="Storage" className="flex flex-col gap-1.5">
          <span className="text-xs text-slate-500 dark:text-slate-400">Storage</span>
          {storages.map((s) => (
            <UsageBar
              key={`${s.node}/${s.name}`}
              label={multiNode ? `${s.name} (${s.node})` : s.name}
              percent={s.value ?? 0}
              text={formatPercent(s.value)}
            />
          ))}
        </section>
      )}
    </CardFrame>
  );
}
