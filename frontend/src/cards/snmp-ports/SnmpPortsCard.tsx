import { useState } from "react";
import type { CardProps } from "../registry";
import { CardFrame } from "../metrics/CardFrame";
import { Figures, KeyFigure } from "../metrics/KeyFigure";
import { StatTile } from "../metrics/StatTile";
import { useCardTier } from "../metrics/tier";
import { useUnits } from "../../app/units";
import { describe, isInternal, speedText, STATE_TEXT, TILE, useSnmpPorts } from "./ports";
import { InternalToggle, PortLegend } from "./PortParts";

export function SnmpPortsCard({ instanceId, title, instance }: CardProps) {
  const { formatRate } = useUnits();
  const { ports: all, loading, error, freshness } = useSnmpPorts(instanceId);
  const [selected, setSelected] = useState<string | null>(null);
  const [showInternal, setShowInternal] = useState(false);

  const physical = all.filter((p) => !isInternal(p));
  const ports = showInternal ? all : physical;
  // Top talkers are physical ports only: a loopback's traffic never left the device.
  const talkers = physical
    .map((p) => ({ port: p, total: (p.inBps ?? 0) + (p.outBps ?? 0) }))
    .filter((t) => t.total > 0)
    .sort((a, b) => b.total - a.total)
    .slice(0, 3);
  const chosen = ports.find((p) => p.index === selected);
  const tier = useCardTier();

  if (tier !== "full") {
    // Ports up, and at medium the busiest port; no port grid.
    const up = physical.filter((p) => p.state === "up").length;
    const top = talkers[0];
    return (
      <CardFrame
        title={title}
        instance={instance}
        loading={loading}
        error={error}
        freshness={freshness}
        empty={all.length === 0}
      >
        {tier === "small" ? (
          <KeyFigure label="Ports up">
            {up}/{physical.length} up
          </KeyFigure>
        ) : (
          <Figures>
            <StatTile label="Ports up" value={`${up}/${physical.length}`} />
            <StatTile
              label="Busiest"
              value={top ? `${top.port.name} · ${formatRate(top.total)}` : "–"}
            />
          </Figures>
        )}
      </CardFrame>
    );
  }

  return (
    <CardFrame
      title={title}
      showTitle={false}
      instance={instance}
      loading={loading}
      error={error}
      freshness={freshness}
      empty={all.length === 0}
    >
      <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1">
        <h3 className="text-xs font-medium text-muted">{title}</h3>
        <PortLegend />
      </div>
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
            className={`flex flex-col items-center rounded border-[1.5px] px-1 py-1 text-xs leading-tight ${TILE[p.state]} ${selected === p.index ? "outline-2 outline-offset-2 outline-in" : ""}`}
          >
            <span className="w-full truncate text-center font-medium">{p.name}</span>
            <span className="tabular-nums text-muted">
              {p.state === "up" ? speedText(p.speed) : STATE_TEXT[p.state]}
            </span>
          </button>
        ))}
      </div>
      <InternalToggle
        count={all.length - physical.length}
        shown={showInternal}
        onToggle={() => setShowInternal(!showInternal)}
      />

      <p className="min-h-5 text-xs text-muted" aria-live="polite">
        {chosen ? describe(chosen, formatRate) : "Tap a port for details."}
      </p>

      {talkers.length > 0 && (
        <section aria-label="Top talkers" className="flex flex-col gap-0.5 text-xs">
          <span className="text-muted">Top talkers</span>
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
