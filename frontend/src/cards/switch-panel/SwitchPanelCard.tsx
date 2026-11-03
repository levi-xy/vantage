import { useLayoutEffect, useState } from "react";
import type { ReactNode } from "react";
import { useUnits } from "../../app/units";
import { CardFrame } from "../metrics/CardFrame";
import { KeyFigure } from "../metrics/KeyFigure";
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
import { faceplate, MIN_SCALE, PLATE, plateScale, plateSize } from "./panel";
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

/** The width and height of an element, kept up to date (undefined until measured). */
function useMeasured(): [
  (el: HTMLElement | null) => void,
  { width: number; height: number } | undefined,
] {
  const [el, setEl] = useState<HTMLElement | null>(null);
  const [size, setSize] = useState<{ width: number; height: number }>();
  useLayoutEffect(() => {
    if (!el || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(([entry]) => {
      if (entry) setSize({ width: entry.contentRect.width, height: entry.contentRect.height });
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, [el]);
  return [setEl, size];
}

/**
 * The switch's front panel: ports where they sit on the device, styled by state. At full size
 * the ports can be tapped for details; smaller, the panel shrinks to fit and the details go.
 */
export function SwitchPanelCard({ instanceId, title, instance }: CardProps) {
  const { formatRate } = useUnits();
  const { ports: all, loading, error, freshness } = useSnmpPorts(instanceId);
  const [selected, setSelected] = useState<string | null>(null);
  const [showInternal, setShowInternal] = useState(false);
  const [measure, room] = useMeasured();
  const tier = useCardTier();
  const full = tier === "full";

  const physical = all.filter((p) => !isInternal(p));
  const hidden = all.length - physical.length;
  const ports = full && showInternal ? all : physical;
  const { copper, sfp } = faceplate(ports);
  const chosen = full ? ports.find((p) => p.index === selected) : undefined;
  const chosenCage = sfp.find((p) => p.port.index === chosen?.index)?.cage;
  const up = physical.filter((p) => p.state === "up").length;
  const top = busiest(physical);

  const natural = plateSize(
    copper.map((b) => b.length),
    sfp.length,
  );
  // Full size grows downwards, so only the width limits it; smaller cards fit both ways.
  const scale = room ? plateScale(natural, full ? { width: room.width } : room) : 1;
  const tooSmall = tier === "small" || (room !== undefined && !full && scale <= MIN_SCALE);
  const px = (n: number) => `${n * scale}px`;
  const labels = scale >= 0.6;

  const tile = ({ port, column, row, cage: number }: Placed<Port>, isCage: boolean) => {
    const on = selected === port.index && full;
    const label = `${number ? `SFP ${number}, ` : ""}${port.name}: ${port.state}`;
    const notch = (
      <span
        aria-hidden="true"
        style={{ width: px(14), height: px(5) }}
        className={`${row === 1 ? "rounded-t-[1px]" : "rounded-b-[1px]"} ${NOTCH[port.state]}`}
      />
    );
    const content = (
      <>
        {!isCage && row === 2 && notch}
        {labels && <span>{number ?? port.index}</span>}
        {!isCage && row === 1 && notch}
      </>
    );
    const style = {
      gridColumn: column,
      gridRow: row,
      width: px(isCage ? PLATE.cageW : PLATE.copperW),
      height: px(PLATE.tileH),
      fontSize: px(11),
    };
    const className = `flex flex-col items-center border-[1.5px] tabular-nums ${TILE[port.state]} ${
      isCage || !labels
        ? "justify-center rounded-[3px]"
        : `justify-between rounded ${row === 1 ? "pt-0.5" : "pb-0.5"}`
    } ${on ? "outline-2 outline-offset-2 outline-in" : ""}`;
    if (!full) {
      return (
        <span
          key={port.index}
          role="img"
          aria-label={label}
          title={describe(port, formatRate)}
          style={style}
          className={className}
        >
          {content}
        </span>
      );
    }
    return (
      <button
        key={port.index}
        type="button"
        style={style}
        aria-pressed={on}
        aria-label={label}
        title={describe(port, formatRate)}
        onClick={() => setSelected(on ? null : port.index)}
        className={className}
      >
        {content}
      </button>
    );
  };

  const plate = (
    <div
      role="group"
      aria-label="Front panel"
      className="flex w-fit flex-nowrap items-center rounded-[10px] border border-line bg-inset"
      style={{ gap: px(PLATE.blockGap), padding: px(PLATE.pad) }}
    >
      {copper.map((block, b) => (
        <div
          key={b}
          className="grid grid-rows-2"
          style={{
            gap: px(PLATE.gap),
            gridTemplateColumns: `repeat(${Math.ceil(block.length / 2)}, auto)`,
          }}
        >
          {block.map((p) => tile(p, false))}
        </div>
      ))}
      {sfp.length > 0 && (
        <div
          aria-label="SFP"
          className={`grid grid-rows-2 ${copper.length ? "border-l border-line" : ""}`}
          style={{
            gap: px(PLATE.gap),
            paddingLeft: copper.length ? px(PLATE.divider - PLATE.border) : undefined,
            gridTemplateColumns: `repeat(${Math.ceil(sfp.length / 2)}, auto)`,
          }}
        >
          {sfp.map((p) => tile(p, true))}
        </div>
      )}
    </div>
  );

  if (!full) {
    return (
      <CardFrame
        title={title}
        instance={instance}
        loading={loading}
        error={error}
        freshness={freshness}
        empty={all.length === 0}
      >
        {/* The panel shrinks to the room under the header; too little room: ports up. */}
        {tier === "small" ? (
          <KeyFigure label="Ports up">
            {up}/{physical.length} up
          </KeyFigure>
        ) : (
          <div ref={measure} className="min-h-0 w-full min-w-0 flex-1 overflow-hidden">
            {tooSmall ? (
              <KeyFigure label="Ports up">
                {up}/{physical.length} up
              </KeyFigure>
            ) : (
              plate
            )}
          </div>
        )}
      </CardFrame>
    );
  }

  // The detail boxes take the panel's width (w-0 min-w-full), so they never widen the card.
  const narrow = natural.width * scale < 480;
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
      <div ref={measure} className="w-full min-w-0">
        <div className="flex w-fit max-w-full flex-col gap-3">
          {plate}
          <div className="w-0 min-w-full">
            <InternalToggle
              count={hidden}
              shown={showInternal}
              onToggle={() => setShowInternal(!showInternal)}
            />
          </div>
          <div
            className={`grid w-0 min-w-full gap-3 ${narrow ? "grid-cols-2" : "grid-cols-4"}`}
            aria-live="polite"
          >
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
        </div>
      </div>
    </CardFrame>
  );
}
