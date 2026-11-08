// The elements of a map, drawn read-only. Every string comes from the saved document or from
// device names, so it is only ever rendered as React text (never as HTML, never as a link).

import { useUnits } from "../../app/units";
import type {
  MapBox,
  MapCable,
  MapDocument,
  MapFrame,
  MapLegend,
  MapTable,
  MapText,
  MapVlan,
} from "../../api";
import { colorVar, deviceColor, isDeviceColor, tint } from "../../app/deviceColors";
import { useProxmoxGuests } from "../../cards/proxmox-guests/useProxmoxGuests";
import { cableLive, endElements } from "./cableLive";
import type { CableLive } from "./cableLive";
import {
  bounds,
  cablePath,
  cableWidth,
  endRects,
  labelPoint,
  pathData,
  SPEED_ORDER,
  speedLabel,
  tagPoint,
} from "./geometry";
import { guestTiles } from "./guests";
import { PortStrip } from "./PortStrip";
import { boxStrip } from "./strip";
import { MapIcon } from "./MapIcon";
import { instanceAddress } from "./mapLive";
import type { MapLive } from "./mapLive";

const paint = (name: string | null | undefined): string | undefined =>
  isDeviceColor(name) ? colorVar(name) : undefined;

const place = (e: { x: number; y: number; w: number; h: number }) => ({
  left: e.x,
  top: e.y,
  width: e.w,
  height: e.h,
});

export function StatusWord({ state }: { state: string | null }) {
  if (state === "up") {
    return <b className="text-[12px] font-bold text-ok-text">UP</b>;
  }
  if (state === "down") {
    return <b className="text-[12px] font-bold text-crit-text">DOWN</b>;
  }
  return null;
}

/** A linked box or frame opens the details drawer: a button for mouse, touch and keyboard. */
function selectable(
  id: string,
  linked: boolean,
  onSelect: ((id: string) => void) | undefined,
  label: string,
) {
  if (!linked || !onSelect) return {};
  return {
    role: "button",
    tabIndex: 0,
    "aria-label": `Details of ${label}`,
    onClick: () => onSelect(id),
    onKeyDown: (e: { key: string; preventDefault: () => void }) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        onSelect(id);
      }
    },
  };
}

export function BoxView({
  box,
  live,
  vlans,
  onSelect,
}: {
  box: MapBox;
  live: MapLive;
  vlans: ReadonlyMap<string, MapVlan>;
  onSelect?: (id: string) => void;
}) {
  const instance = live.instance(box.instance_id);
  const state = live.state(box.instance_id);
  const linked = box.instance_id !== null;
  const removed = state === "removed";
  const down = state === "down";
  const color = paint(box.color) ?? deviceColor(instance);
  const dashed = !linked || removed;
  const name = box.label || instance?.name || "";
  const strip = boxStrip(box, live.ports(box.instance_id));

  const border = down ? "border-crit" : "border-line";
  let body;
  if (removed) {
    body = (
      <>
        <b className="block truncate text-muted">{box.label || "Device"}</b>
        <span className="block text-[12px] text-muted">Device removed</span>
      </>
    );
  } else if (instance) {
    const figures = box.show.figures ? live.figures(box, instance) : [];
    body = (
      <>
        <span className="block truncate">
          <b className={`text-[15px] ${down ? "text-crit-text" : ""}`}>{name}</b>
          {box.show.status && (
            <>
              {" "}
              <StatusWord state={state} />
            </>
          )}
        </span>
        {box.show.details && (
          <span className="block truncate font-mono text-[11px] text-muted">
            {[live.typeName(instance.type_id), instanceAddress(instance)]
              .filter(Boolean)
              .join(" · ")}
          </span>
        )}
        {figures.length > 0 && (
          <span className="block truncate text-[12px] text-muted">
            {figures.map(({ def, text }, i) => (
              <span key={def.id}>
                {i > 0 && " · "}
                {def.label}{" "}
                <b
                  className={down ? "text-crit-text" : "text-ink"}
                  style={{ color: down ? undefined : color }}
                >
                  {text}
                </b>
              </span>
            ))}
          </span>
        )}
      </>
    );
  } else {
    // Unlinked, or the instance list has not loaded yet.
    body = (
      <>
        <b className="block truncate text-[15px]">{box.label}</b>
        <span className="block truncate text-[12px] text-muted">
          {linked ? "" : box.subtitle || "Not monitored"}
        </span>
      </>
    );
  }

  return (
    <div
      data-element={box.id}
      data-kind="box"
      data-down={down ? "true" : undefined}
      className={`absolute box-border flex items-start gap-2.5 overflow-hidden rounded-[10px] border bg-panel px-3 py-2.5 text-ink ${border} ${dashed ? "border-dashed" : ""} ${instance && onSelect ? "cursor-pointer focus-visible:ring-2 focus-visible:ring-in focus-visible:outline-none" : ""}`}
      style={place(box)}
      {...selectable(box.id, instance !== undefined && !removed, onSelect, name)}
    >
      {color && instance && !removed && (
        <span
          aria-hidden="true"
          className="absolute inset-x-0 top-0 h-[3px]"
          style={{ background: down ? "var(--vt-crit)" : color }}
        />
      )}
      <span
        aria-hidden="true"
        className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-chip text-muted"
        style={color && instance && !removed ? { background: tint(color), color } : undefined}
      >
        <MapIcon icon={box.icon} />
      </span>
      <span className="min-w-0 flex-1">{body}</span>
      {strip && <PortStrip layout={strip.layout} origin={strip.origin} vlans={vlans} />}
    </div>
  );
}

export function FrameView({
  frame,
  live,
  onSelect,
}: {
  frame: MapFrame;
  live: MapLive;
  onSelect?: (id: string) => void;
}) {
  const instance = live.instance(frame.instance_id);
  const state = live.state(frame.instance_id);
  const down = state === "down";
  const color = paint(frame.color) ?? deviceColor(instance);
  return (
    <div
      data-element={frame.id}
      data-kind="frame"
      data-down={down ? "true" : undefined}
      className={`absolute box-border overflow-hidden rounded-xl border px-4 py-3 focus-visible:ring-2 focus-visible:ring-in focus-visible:outline-none ${down ? "border-crit" : "border-line"} ${instance && onSelect ? "cursor-pointer" : ""}`}
      {...selectable(
        frame.id,
        instance !== undefined && state !== "removed",
        onSelect,
        frame.label || instance?.name || "",
      )}
      style={{
        ...place(frame),
        borderTop: `3px solid ${down ? "var(--vt-crit)" : (color ?? "var(--vt-line)")}`,
        background: color ? tint(color, 4) : undefined,
      }}
    >
      <div className="truncate">
        <b className={`text-[15px] ${down ? "text-crit-text" : ""}`}>
          {frame.label || instance?.name || ""}
        </b>{" "}
        <StatusWord state={state} />
      </div>
      {instance?.type_id === "proxmox" && <FrameGuests frame={frame} instanceId={instance.id} />}
      {state === "removed" && <div className="text-[12px] text-muted">Device removed</div>}
    </div>
  );
}

function FrameGuests({ frame, instanceId }: { frame: MapFrame; instanceId: string }) {
  const { guests } = useProxmoxGuests(instanceId);
  const tiles = guestTiles(guests, frame.guests);
  return (
    <div
      className="mt-2 grid gap-2 overflow-hidden"
      style={{ gridTemplateColumns: "repeat(auto-fill, 150px)", maxHeight: frame.h - 52 }}
    >
      {tiles.map((t) => (
        <div
          key={t.key}
          data-guest={t.key}
          className={`box-border h-[54px] overflow-hidden rounded-lg border bg-inset px-2.5 py-2 text-[12px] text-muted ${t.running === false ? "border-dashed border-line" : "border-line"}`}
        >
          <b className={`text-[14px] ${t.running === false ? "text-muted" : "text-ink"}`}>
            {t.name}
          </b>{" "}
          {t.running === true && <b className="text-ok-text">UP</b>}
          {t.running === false && <span>Stopped</span>}
          <span className="block truncate">{t.subtitle}</span>
        </div>
      ))}
    </div>
  );
}

const fontSize = { s: 12, m: 14, l: 18 } as const;

export function TextView({ el }: { el: MapText }) {
  return (
    <div
      data-element={el.id}
      data-kind="text"
      className={`absolute box-border overflow-hidden whitespace-pre-wrap text-ink ${el.panel ? "rounded-[10px] border border-line bg-panel p-3.5" : ""}`}
      style={{ ...place(el), fontSize: fontSize[el.size] }}
    >
      {el.text}
    </div>
  );
}

export function TableView({ el }: { el: MapTable }) {
  return (
    <div
      data-element={el.id}
      data-kind="table"
      className="absolute box-border overflow-hidden rounded-[10px] border border-line bg-panel px-4 py-3.5"
      style={place(el)}
    >
      {el.title && <b className="mb-2 block text-[15px]">{el.title}</b>}
      <table className="w-full border-collapse text-[13px]">
        {el.columns.length > 0 && (
          <thead>
            <tr className="text-left text-[12px] text-muted">
              <th className="w-3.5" />
              {el.columns.map((c, i) => (
                <th key={i} className="py-1 pr-2 font-semibold">
                  {c}
                </th>
              ))}
            </tr>
          </thead>
        )}
        <tbody>
          {el.rows.map((row, r) => (
            <tr key={r} className="border-t border-line">
              <td className="py-1.5">
                {paint(row.color) && (
                  <span
                    aria-hidden="true"
                    className="block h-2.5 w-2.5 rounded-[2px]"
                    style={{ background: paint(row.color) }}
                  />
                )}
              </td>
              {row.cells.map((cell, c) => (
                <td key={c} className="py-1.5 pr-2">
                  {cell}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** Speeds, line styles, roles and VLANs that the cables of this map actually use. */
function legendEntries(doc: MapDocument) {
  const cables = doc.elements.filter((e): e is MapCable => e.kind === "cable");
  const speeds = SPEED_ORDER.filter((s) => cables.some((c) => c.speed === s));
  const styles = (["dotted", "dashed"] as const).filter((s) => cables.some((c) => c.style === s));
  const roles = (["access", "trunk"] as const).filter((r) => cables.some((c) => c.role === r));
  const usedVlans = new Set(cables.map((c) => c.vlan));
  const vlans = doc.vlans.filter((v) => usedVlans.has(v.id));
  return { speeds, styles, roles, vlans };
}

function Sample({ width, dash, trunk }: { width: number; dash?: string; trunk?: boolean }) {
  return (
    <svg width="44" height="10" viewBox="0 0 44 10" aria-hidden="true">
      {trunk && (
        <path d="M 2 5 L 42 5" stroke="var(--vt-muted)" strokeWidth={width + 3} opacity="0.5" />
      )}
      <path
        d="M 2 5 L 42 5"
        stroke={trunk ? "var(--vt-ink)" : "var(--vt-muted)"}
        strokeWidth={width}
        strokeDasharray={dash}
        strokeLinecap="round"
        fill="none"
      />
    </svg>
  );
}

const DASH = { solid: undefined, dotted: "1.5 4", dashed: "7 5" } as const;

export function LegendView({ el, doc }: { el: MapLegend; doc: MapDocument }) {
  const { speeds, styles, roles, vlans } = legendEntries(doc);
  const row = "flex items-center gap-3";
  return (
    <div
      data-element={el.id}
      data-kind="legend"
      className="absolute box-border flex flex-col gap-2 overflow-hidden rounded-[10px] border border-line bg-panel px-4 py-3.5 text-[13px]"
      style={place(el)}
    >
      <b className="text-[15px]">Legend</b>
      {speeds.map((s) => (
        <span key={s} className={row}>
          <Sample width={cableWidth(s)} />
          {speedLabel(s)}
        </span>
      ))}
      {styles.map((s) => (
        <span key={s} className={row}>
          <Sample width={1.5} dash={DASH[s]} />
          {s === "dotted" ? "Dotted" : "Dashed"}
        </span>
      ))}
      {roles.map((r) => (
        <span key={r} className={row}>
          <Sample width={1.5} trunk={r === "trunk"} />
          {r === "trunk" ? "Trunk" : "Access"}
        </span>
      ))}
      {vlans.map((v) => (
        <span key={v.id} className={row}>
          <span
            aria-hidden="true"
            className="ml-1 block h-2.5 w-2.5 rounded-[2px]"
            style={{ background: paint(v.color) }}
          />
          VLAN {v.id} · {v.name}
        </span>
      ))}
    </div>
  );
}

function cableColor(cable: MapCable, doc: MapDocument): string {
  const named = paint(cable.color);
  if (named) return named;
  if (cable.role === "access" && cable.vlan) {
    const v = doc.vlans.find((x) => x.id === cable.vlan);
    if (v) return paint(v.color) ?? "var(--vt-muted)";
  }
  return "var(--vt-muted)";
}

export interface CableHover {
  id: string;
  /** Pointer position in the drawing's viewport (not zoomed). */
  x: number;
  y: number;
}

/** "↓ 1.2 Mbit/s ↑ 340 kbit/s" in the app's in (blue) and out (orange) colours. */
export function RateText({ rate }: { rate: NonNullable<CableLive["rate"]> }) {
  const { formatRate } = useUnits();
  return (
    <>
      <tspan fill="var(--vt-in)">↓ {formatRate(rate.inBps)}</tspan>
      <tspan fill="var(--vt-out)" dx="6">
        ↑ {formatRate(rate.outBps)}
      </tspan>
    </>
  );
}

export function CablesLayer({
  doc,
  live,
  onHover,
}: {
  doc: MapDocument;
  live: MapLive;
  onHover?: (hover: CableHover | null) => void;
}) {
  const rects = endRects(doc);
  const owners = endElements(doc);
  const b = bounds(doc);
  const cables = doc.elements.filter((e): e is MapCable => e.kind === "cable");
  // A cable that names a port of a box with a strip meets that jack; otherwise the box edge.
  const strips = new Map<string, ReturnType<typeof boxStrip>>();
  for (const el of owners.values()) {
    if (el.kind === "box") strips.set(el.id, boxStrip(el, live.ports(el.instance_id)));
  }
  const endRect = (end: MapCable["from"]) =>
    (end.port !== null ? strips.get(end.element)?.rects.get(end.port) : undefined) ??
    rects.get(end.element);
  return (
    <svg
      aria-hidden="true"
      className="pointer-events-none absolute top-0 left-0 overflow-visible"
      width={b.x + b.w + 40}
      height={b.y + b.h + 40}
      fill="none"
    >
      {cables.map((cable) => {
        const [boxA, boxB] = [rects.get(cable.from.element), rects.get(cable.to.element)];
        const [a, z] = [endRect(cable.from), endRect(cable.to)];
        if (!boxA || !boxB || !a || !z) return null;
        const pts = cablePath(a, z, cable.bends, boxA, boxB);
        const d = pathData(pts);
        const state = cableLive(cable, owners, live);
        const down = state.down;
        const w = cableWidth(cable.speed);
        const color = down ? "var(--vt-crit)" : cableColor(cable, doc);
        const dash = down ? "6 5" : DASH[cable.style];
        const [lx, ly] = labelPoint(pts);
        const [tx, ty] = tagPoint(pts);
        const text = {
          fill: down ? "var(--vt-crit-text)" : "var(--vt-muted)",
          stroke: "var(--vt-canvas)",
          strokeWidth: 3,
          paintOrder: "stroke",
          fontSize: 12,
        } as const;
        const rate = cable.show_rate ? state.rate : null;
        return (
          <g key={cable.id} data-cable={cable.id} data-down={down ? "true" : undefined}>
            {cable.role === "trunk" && !down && (
              <path
                d={d}
                stroke="var(--vt-muted)"
                strokeWidth={w + 3}
                opacity="0.5"
                strokeDasharray={dash}
                strokeLinejoin="round"
              />
            )}
            <path
              d={d}
              stroke={cable.role === "trunk" && !down ? "var(--vt-ink)" : color}
              strokeWidth={w}
              strokeDasharray={dash}
              strokeLinecap={cable.style === "dotted" ? "round" : "butt"}
              strokeLinejoin="round"
            />
            {onHover && (
              <path
                d={d}
                data-hit={cable.id}
                stroke="transparent"
                strokeWidth={Math.max(14, w + 8)}
                pointerEvents="stroke"
                onPointerMove={(e) => onHover({ id: cable.id, x: e.clientX, y: e.clientY })}
                onPointerLeave={() => onHover(null)}
              />
            )}
            {(cable.label || rate) && (
              <text x={lx + 6} y={ly - 6} {...text}>
                {cable.label}
                {cable.label && rate ? " " : ""}
                {rate && <RateText rate={rate} />}
              </text>
            )}
            {cable.speed && (
              <text x={tx} y={ty} textAnchor="middle" {...text} fontSize={11}>
                {speedLabel(cable.speed)}
              </text>
            )}
          </g>
        );
      })}
    </svg>
  );
}
