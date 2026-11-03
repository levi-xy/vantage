import { useState } from "react";
import type { ReactNode } from "react";
import { useUnits } from "../../app/units";
import { CardFrame } from "../metrics/CardFrame";
import { Figures, KeyFigure } from "../metrics/KeyFigure";
import { StatTile } from "../metrics/StatTile";
import { useCardTier } from "../metrics/tier";
import type { CardProps } from "../registry";
import {
  busiest,
  describe,
  isInternal,
  NOTCH,
  speedText,
  STATE_TEXT,
  TILE,
  useSnmpPorts,
} from "../snmp-ports/ports";
import type { Port } from "../snmp-ports/ports";
import { InternalToggle, PortLegend } from "../snmp-ports/PortParts";
import { faceplate } from "./panel";
import type { Placed } from "./panel";

function Box({ label, children, wide }: { label: string; children: ReactNode; wide?: boolean }) {
  return (
    <div
      className={`flex min-w-0 flex-col gap-1 rounded-[10px] border border-line bg-inset px-3.5 py-3 ${wide ? "col-span-2" : ""}`}
    >
      <div className="truncate text-xs text-muted">{label}</div>
      <div className="truncate text-[15px] tabular-nums">{children}</div>
    </div>
  );
}

/** The switch's front panel: ports where they sit on the device, styled by state. */
export function SwitchPanelCard({ instanceId, title, instance }: CardProps) {
  const { formatRate } = useUnits();
  const { ports: all, loading, error, freshness } = useSnmpPorts(instanceId);
  const [selected, setSelected] = useState<string | null>(null);
  const [showInternal, setShowInternal] = useState(false);

  const physical = all.filter((p) => !isInternal(p));
  const hidden = all.length - physical.length;
  const ports = showInternal ? all : physical;
  const { copper, sfp } = faceplate(ports);
  const chosen = ports.find((p) => p.index === selected);
  const chosenCage = sfp.find((p) => p.port.index === selected)?.cage;
  const up = physical.filter((p) => p.state === "up").length;
  const top = busiest(physical);
  const tier = useCardTier();

  if (tier !== "full") {
    // Ports up, and at medium the busiest port; no front panel.
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

  const tile = ({ port, column, row, cage: number }: Placed<Port>, cage: boolean) => {
    const on = selected === port.index;
    const notch = (
      <span
        aria-hidden="true"
        className={`h-[5px] w-3.5 ${row === 1 ? "rounded-t-[1px]" : "rounded-b-[1px]"} ${NOTCH[port.state]}`}
      />
    );
    return (
      <button
        key={port.index}
        type="button"
        style={{ gridColumn: column, gridRow: row }}
        aria-pressed={on}
        aria-label={`${number ? `SFP ${number}, ` : ""}${port.name}: ${port.state}`}
        title={describe(port, formatRate)}
        onClick={() => setSelected(on ? null : port.index)}
        className={`flex h-8 flex-col items-center border-[1.5px] text-[11px] tabular-nums ${TILE[port.state]} ${
          cage
            ? "w-[42px] justify-center rounded-[3px]"
            : `w-[34px] justify-between rounded ${row === 1 ? "pt-0.5" : "pb-0.5"}`
        } ${on ? "outline-2 outline-offset-2 outline-in" : ""}`}
      >
        {!cage && row === 2 && notch}
        <span>{number ?? port.index}</span>
        {!cage && row === 1 && notch}
      </button>
    );
  };

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
        aria-label="Front panel"
        className="flex w-fit max-w-full flex-wrap items-center gap-4 rounded-[10px] border border-line bg-inset p-3"
      >
        {copper.map((block, b) => (
          <div
            key={b}
            className="grid grid-rows-2 gap-[5px]"
            style={{ gridTemplateColumns: `repeat(${Math.ceil(block.length / 2)}, auto)` }}
          >
            {block.map((p) => tile(p, false))}
          </div>
        ))}
        {sfp.length > 0 && (
          <div
            aria-label="SFP"
            className={`grid grid-rows-2 gap-[5px] ${copper.length ? "border-l border-line pl-3" : ""}`}
            style={{ gridTemplateColumns: `repeat(${Math.ceil(sfp.length / 2)}, auto)` }}
          >
            {sfp.map((p) => tile(p, true))}
          </div>
        )}
      </div>
      <InternalToggle
        count={hidden}
        shown={showInternal}
        onToggle={() => setShowInternal(!showInternal)}
      />
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4" aria-live="polite">
        <Box
          wide
          label={
            chosen
              ? [
                  chosenCage ? `SFP ${chosenCage}` : `Port ${chosen.index}`,
                  chosen.name,
                  chosen.alias,
                  speedText(chosen.speed),
                  STATE_TEXT[chosen.state],
                ]
                  .filter((s) => s && s !== "–")
                  .join(" · ")
              : "Selected port"
          }
        >
          {chosen ? (
            <>
              <span className="text-in">↓</span> {formatRate(chosen.inBps)}
              <span className="ml-3 text-out">↑</span> {formatRate(chosen.outBps)}
            </>
          ) : (
            <span className="text-sm text-muted">Tap a port for details.</span>
          )}
        </Box>
        <Box label="Ports up">
          {up} of {physical.length}
        </Box>
        <Box label="Busiest">{top ? `${top.port.name} · ${formatRate(top.total)}` : "–"}</Box>
      </div>
    </CardFrame>
  );
}
