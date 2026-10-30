import { useState } from "react";
import type { CardProps } from "../registry";
import { CardFrame } from "../metrics/CardFrame";
import { useUnits } from "../../app/units";
import { describe, speedText, TILE, useSnmpPorts } from "./ports";

export function SnmpPortsCard({ instanceId, title, instance }: CardProps) {
  const { formatRate } = useUnits();
  const { ports, loading, error } = useSnmpPorts(instanceId);
  const [selected, setSelected] = useState<string | null>(null);

  const talkers = ports
    .map((p) => ({ port: p, total: (p.inBps ?? 0) + (p.outBps ?? 0) }))
    .filter((t) => t.total > 0)
    .sort((a, b) => b.total - a.total)
    .slice(0, 3);
  const chosen = ports.find((p) => p.index === selected);

  return (
    <CardFrame
      title={title}
      instance={instance}
      loading={loading}
      error={error}
      empty={ports.length === 0}
    >
      <div
        role="group"
        aria-label="Ports"
        className="grid grid-cols-[repeat(auto-fill,minmax(3.5rem,1fr))] gap-1.5"
      >
        {ports.map((p) => (
          <button
            key={p.index}
            type="button"
            aria-pressed={selected === p.index}
            aria-label={`${p.name}: ${p.state}`}
            title={describe(p, formatRate)}
            onClick={() => setSelected(selected === p.index ? null : p.index)}
            className={`flex flex-col items-center rounded border px-1 py-1 text-xs leading-tight ${TILE[p.state]} ${selected === p.index ? "ring-2 ring-blue-500" : ""}`}
          >
            <span className="w-full truncate text-center font-medium">{p.name}</span>
            <span className="tabular-nums opacity-80">
              {p.state === "up" ? speedText(p.speed) : p.state}
            </span>
          </button>
        ))}
      </div>

      <p className="min-h-5 text-xs text-slate-600 dark:text-slate-300" aria-live="polite">
        {chosen ? describe(chosen, formatRate) : "Tap a port for details."}
      </p>

      {talkers.length > 0 && (
        <section aria-label="Top talkers" className="flex flex-col gap-0.5 text-xs">
          <span className="text-slate-500 dark:text-slate-400">Top talkers</span>
          {talkers.map(({ port, total }) => (
            <div key={port.index} className="flex justify-between gap-2">
              <span className="truncate">{port.name}</span>
              <span className="tabular-nums">{formatRate(total)}</span>
            </div>
          ))}
        </section>
      )}
    </CardFrame>
  );
}
