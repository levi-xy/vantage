import type { CardProps } from "../registry";
import { cardFreshness } from "../metrics/freshness";
import { CardFrame } from "../metrics/CardFrame";
import { formatPercent } from "../metrics/format";
import { byLabel, firstValue } from "../metrics/promql";
import { useInstantQuery } from "../metrics/useMetricQuery";
import { StatTile } from "../metrics/StatTile";
import { Figures, KeyFigure } from "../metrics/KeyFigure";
import { useCardTier } from "../metrics/tier";

// Polled by Vantage (integrations/builtin/proxmox): vantage_proxmox_guest_*.
// The guest's name and tags are free text, so they are only on `guest_info`: join on `vmid`.
const P = "vantage_proxmox_";
type Sel = (extra?: string) => string;
// Chips shown after a guest's name; the rest become "+N" (all of them in its tooltip).
const MAX_CHIPS = 3;

/** `guest_tags` is ";"-joined, sorted and cleaned by the backend; "" or absent = none. */
function parseTags(raw: string | undefined): string[] {
  return (raw ?? "").split(";").filter((t) => t !== "");
}

function TagChips({ tags }: { tags: string[] }) {
  if (tags.length === 0) return null;
  const shown = tags.slice(0, MAX_CHIPS);
  const rest = tags.length - shown.length;
  const chip = "max-w-[8rem] truncate rounded bg-chip px-1.5 py-px text-[11px] text-muted";
  return (
    <span className="flex min-w-0 shrink items-center gap-1" aria-label="Tags">
      {shown.map((t) => (
        <span key={t} className={chip} title={t}>
          {t}
        </span>
      ))}
      {rest > 0 && (
        <span className={`${chip} shrink-0`} title={tags.join(", ")}>
          +{rest}
        </span>
      )}
    </span>
  );
}

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
  const infoBy = new Map(byLabel(info.data, "vmid").map((x) => [x.key, x.labels]));

  const guests = byLabel(running.data, "vmid")
    .map((g) => ({
      vmid: g.key,
      node: g.labels.node ?? "",
      type: g.labels.guest_type ?? "",
      name: infoBy.get(g.key)?.guest_name || `Guest ${g.key}`,
      tags: parseTags(infoBy.get(g.key)?.guest_tags),
      running: g.value === 1,
    }))
    .sort((a, b) => Number(b.running) - Number(a.running) || Number(a.vmid) - Number(b.vmid));
  const omittedCount = firstValue(omitted.data) ?? 0;
  const tier = useCardTier();
  const runningCount = guests.filter((g) => g.running).length;

  return (
    <CardFrame
      title={title}
      instance={instance}
      loading={running.isPending}
      error={running.error}
      freshness={cardFreshness(running, cpu, mem, info, omitted)}
      empty={guests.length === 0}
      emptyHint="No guests reported yet."
    >
      {tier === "small" ? (
        <KeyFigure label="Guests running">
          {runningCount}/{guests.length} running
        </KeyFigure>
      ) : tier === "medium" ? (
        <Figures>
          <StatTile label="Running" value={String(runningCount)} />
          <StatTile label="Stopped" value={String(guests.length - runningCount)} />
        </Figures>
      ) : (
        <>
          <ul className="flex flex-col divide-y divide-slate-200 dark:divide-slate-800">
            {guests.map((g) => (
              <li key={g.vmid} className="flex items-center gap-2 py-1.5 text-sm">
                <span className="min-w-0 flex-1">
                  <span className="flex min-w-0 items-center gap-1.5">
                    <span className="min-w-0 truncate font-medium">{g.name}</span>
                    <TagChips tags={g.tags} />
                  </span>
                  <span className="block text-xs text-slate-500 dark:text-slate-400">
                    {g.vmid} · {g.type} · {g.node}
                  </span>
                </span>
                {g.running ? (
                  <span className="shrink-0 text-right text-xs tabular-nums">
                    <span className="block">CPU {formatPercent(cpuBy.get(g.vmid))}</span>
                    <span className="block">Mem {formatPercent(memBy.get(g.vmid))}</span>
                  </span>
                ) : (
                  <span className="shrink-0 text-xs text-muted">stopped</span>
                )}
              </li>
            ))}
          </ul>
          {omittedCount > 0 && (
            <p className="text-xs text-slate-500 dark:text-slate-400">
              {omittedCount} more guests not shown (limit set in the integration).
            </p>
          )}
        </>
      )}
    </CardFrame>
  );
}
