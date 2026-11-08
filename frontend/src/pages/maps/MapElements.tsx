// The elements of a map, drawn read-only. Every string comes from the saved document or from
// device names, so it is only ever rendered as React text (never as HTML, never as a link).

import { useUnits } from "../../app/units";
import type { MapCable, MapDocument, MapLegend, MapNetworks, MapTable, MapText } from "../../api";
import { cableLive, endElements } from "./cableLive";
import { cableRoutes } from "./cableRoutes";
import type { CableLive } from "./cableLive";
import { bounds, cableWidth, labelPoint, pathData, speedLabel, tagPoint } from "./geometry";
import { legendEntries, STYLE_NAME } from "./legend";
import { BoxView } from "./BoxView";
import { paint } from "./boxFacts";
import { Clip, StatusWord } from "./boxParts";
import { FrameView } from "./FrameView";
import type { MapLive } from "./mapLive";
import { useReportHeight } from "./measure";

const place = (e: { x: number; y: number; w: number; h: number }) => ({
  left: e.x,
  top: e.y,
  width: e.w,
  height: e.h,
});

export { BoxView, FrameView, StatusWord };

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
                  {(el.badges ?? []).includes(c) && cell !== "" ? (
                    <span
                      data-badge=""
                      className="inline-block rounded-full border border-in px-2 py-px text-[11px] font-bold text-in"
                    >
                      {cell}
                    </span>
                  ) : (
                    cell
                  )}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Sample({
  width,
  dash,
  trunk,
  color,
}: {
  width: number;
  dash?: string;
  trunk?: boolean;
  color?: string;
}) {
  return (
    <svg width="44" height="10" viewBox="0 0 44 10" aria-hidden="true">
      {trunk && (
        <path d="M 2 5 L 42 5" stroke="var(--vt-muted)" strokeWidth={width + 3} opacity="0.5" />
      )}
      <path
        d="M 2 5 L 42 5"
        stroke={trunk ? "var(--vt-ink)" : (color ?? "var(--vt-muted)")}
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
  const ref = useReportHeight(el.id);
  const { speeds, styles, roles, vlans, portVlans, liveFigure, networks } = legendEntries(doc);
  const row = "flex items-center gap-3";
  return (
    <div
      ref={ref}
      data-element={el.id}
      data-kind="legend"
      className="absolute box-border flex flex-col gap-2 rounded-[10px] border border-line bg-panel px-4 py-3.5 text-[13px]"
      style={{ left: el.x, top: el.y, width: el.w }}
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
          {STYLE_NAME[s]}
        </span>
      ))}
      {roles.map((r) => (
        <span key={r} className={row}>
          <Sample
            width={r === "trunk" ? 1.5 : 2.5}
            trunk={r === "trunk"}
            color={r === "access" ? "var(--vt-in)" : undefined}
          />
          {r === "trunk" ? "Trunk (tagged VLANs)" : "Access port, in its VLAN colour"}
        </span>
      ))}
      {portVlans && (
        <span data-legend="port-vlans" className={row}>
          <svg width="44" height="10" viewBox="0 0 44 10" aria-hidden="true">
            <rect x="14" y="3" width="5" height="7" rx="1" fill="var(--vt-in)" />
            <rect x="21" y="6" width="5" height="4" rx="1" fill="var(--vt-out)" />
            <rect x="28" y="6" width="5" height="4" rx="1" fill="var(--vt-muted)" />
          </svg>
          Port VLANs · tall = untagged
        </span>
      )}
      {liveFigure && (
        <span data-legend="live-figure" className={row}>
          <span className="flex w-11 justify-center">
            <span aria-hidden="true" className="block h-1.5 w-1.5 rounded-full bg-ok" />
          </span>
          Live figure from Vantage
        </span>
      )}
      {!networks &&
        vlans.map((v) => (
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

/** The map's VLANs as a panel: colour bar, "VLAN id", name, note and subnet. */
export function NetworksView({ el, doc }: { el: MapNetworks; doc: MapDocument }) {
  const ref = useReportHeight(el.id);
  return (
    <div
      ref={ref}
      data-element={el.id}
      data-kind="networks"
      className="absolute box-border flex flex-col gap-2 rounded-[10px] border border-line bg-panel px-4 py-3.5"
      style={{ left: el.x, top: el.y, width: el.w }}
    >
      <b className="text-[15px]">Networks</b>
      {doc.vlans.length === 0 && <span className="text-[12px] text-muted">No VLANs yet.</span>}
      {doc.vlans.map((v) => {
        const c = paint(v.color) ?? "var(--vt-muted)";
        return (
          <div
            key={v.id}
            data-vlan-row={v.id}
            className="flex min-w-0 items-stretch overflow-hidden rounded-md border border-line bg-inset"
          >
            <span aria-hidden="true" className="w-1 shrink-0" style={{ background: c }} />
            <span className="min-w-0 flex-1 px-2.5 py-1.5 leading-[16px]">
              <span className="flex min-w-0 items-baseline gap-2">
                <span className="flex min-w-0 flex-1 items-baseline gap-1.5">
                  <span
                    className="shrink-0 font-mono text-[10px] font-bold tracking-wide uppercase"
                    style={{ color: c }}
                  >
                    VLAN {v.id}
                  </span>
                  <Clip className="text-[13px] font-bold">{v.name}</Clip>
                </span>
                {v.subnet && (
                  <Clip className="max-w-[45%] shrink-0 font-mono text-[11px] text-muted">
                    {v.subnet}
                  </Clip>
                )}
              </span>
              {v.note && <Clip className="text-[11px] text-muted">{v.note}</Clip>}
            </span>
          </div>
        );
      })}
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
  onPick,
  pickedId,
}: {
  doc: MapDocument;
  live: MapLive;
  onHover?: (hover: CableHover | null) => void;
  /** The editor: a click on a cable selects it. */
  onPick?: (id: string) => void;
  pickedId?: string | null;
}) {
  const owners = endElements(doc);
  const b = bounds(doc);
  const cables = doc.elements.filter((e): e is MapCable => e.kind === "cable");
  const routes = cableRoutes(doc, live);
  return (
    <svg
      className="pointer-events-none absolute top-0 left-0 overflow-visible"
      width={b.x + b.w + 40}
      height={b.y + b.h + 40}
      fill="none"
    >
      {cables.map((cable) => {
        const pts = routes.points(cable);
        if (!pts) return null;
        const d = pathData(pts);
        const state = cableLive(cable, owners, live);
        const down = state.down;
        const w = cableWidth(cable.speed);
        const color = down ? "var(--vt-crit)" : cableColor(cable, doc);
        const dash = down ? "6 5" : DASH[cable.style];
        const [lx, ly] = routes.labelAt(cable) ?? labelPoint(pts);
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
            {pickedId === cable.id && (
              <path
                d={d}
                data-picked="true"
                stroke="var(--vt-in)"
                strokeWidth={w + 6}
                opacity="0.35"
                strokeLinejoin="round"
              />
            )}
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
            {(onHover || onPick) && (
              <path
                d={d}
                data-hit={cable.id}
                stroke="transparent"
                strokeWidth={Math.max(14, w + 8)}
                pointerEvents="stroke"
                // Reachable by keyboard: focus shows the same card as hovering.
                role="button"
                tabIndex={0}
                aria-label={`Cable ${state.ends[0].device} to ${state.ends[1].device}`}
                className="focus-visible:outline-none"
                onPointerMove={(e) => onHover?.({ id: cable.id, x: e.clientX, y: e.clientY })}
                onPointerLeave={() => onHover?.(null)}
                onFocus={(e) => {
                  const r = e.currentTarget.getBoundingClientRect();
                  onHover?.({ id: cable.id, x: r.left + r.width / 2, y: r.top + r.height / 2 });
                }}
                onBlur={() => onHover?.(null)}
                onClick={() => onPick?.(cable.id)}
                onKeyDown={(e) => {
                  if (onPick && (e.key === "Enter" || e.key === " ")) {
                    e.preventDefault();
                    onPick(cable.id);
                  }
                }}
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
