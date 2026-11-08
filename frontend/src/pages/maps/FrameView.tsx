// A frame: the tab on the top edge (name, kind, status), an optional left column with the
// device's identity and sections, and the guests as titled columns. The height follows the
// content (a frame with nothing to show keeps its stored height). Every string is rendered as
// React text; long text is cut with "…" and the full text is the hover title.

import type { MapBox, MapFrame } from "../../api";
import { deviceColor, tint } from "../../app/deviceColors";
import { useProxmoxGuests } from "../../cards/proxmox-guests/useProxmoxGuests";
import { boxIdentity, frameSized, paint, selectable } from "./boxFacts";
import { Clip, IdentityBlock, SectionsView, StatusWord } from "./boxParts";
import type { BoxModel } from "./boxParts";
import { guestColumns, guestTiles } from "./guests";
import type { GuestTile } from "./guests";
import type { MapLive } from "./mapLive";
import { useReportHeight } from "./measure";

/** The frame's side column as a box, so the box's identity and sections components draw it. */
function sideBox(frame: MapFrame): MapBox {
  const side = frame.side;
  return {
    id: frame.id,
    kind: "box",
    x: frame.x,
    y: frame.y,
    w: frame.w,
    h: frame.h,
    label: frame.label,
    subtitle: "",
    shape: "card",
    role: "",
    icon: "server",
    model: side?.model || frame.model,
    address: side?.address || frame.address,
    version: side?.version || frame.version,
    instance_id: frame.instance_id,
    show: { details: true, status: false, figures: false },
    figures: [],
    sections: side?.sections ?? [],
    color: frame.color,
    panel: null,
    nas: null,
  };
}

type Usage = ReadonlyMap<string, number | undefined>;
const EMPTY: Usage = new Map();

function Bar({ label, value, color }: { label: string; value: number; color: string }) {
  const pct = Math.max(0, Math.min(100, value));
  return (
    <span data-bar={label.toLowerCase()} className="block min-w-0">
      <span className="block text-[9px] leading-3 font-bold tracking-wide text-muted uppercase">
        {label}
      </span>
      <span
        role="img"
        aria-label={`${label} ${Math.round(pct)} %`}
        className="block h-[3px] overflow-hidden rounded-full bg-line"
      >
        <span
          className="block h-full rounded-full"
          style={{ width: `${pct}%`, background: color }}
        />
      </span>
    </span>
  );
}

function Tile({
  tile,
  bars,
  color,
  cpu,
  mem,
}: {
  tile: GuestTile;
  bars: boolean;
  color: string;
  cpu: Usage;
  mem: Usage;
}) {
  const stopped = tile.running === false;
  const cpuV = cpu.get(tile.vmid);
  const memV = mem.get(tile.vmid);
  const showBars = bars && tile.running === true && (cpuV !== undefined || memV !== undefined);
  return (
    <div
      data-guest={tile.key}
      className={`box-border min-w-0 rounded-lg border bg-inset px-2.5 py-1.5 text-[12px] ${stopped ? "border-dashed border-line text-muted" : "border-line"}`}
    >
      <div className="flex min-w-0 items-baseline gap-2">
        <Clip className={`flex-1 text-[13px] font-bold ${stopped ? "text-muted" : "text-ink"}`}>
          {tile.name}
        </Clip>
        {tile.ref && <span className="shrink-0 text-[11px] text-muted">{tile.ref}</span>}
      </div>
      <div className="flex min-w-0 items-baseline gap-2 leading-[16px]">
        {tile.running === true && <b className="shrink-0 text-ok-text">UP</b>}
        {stopped && <span className="shrink-0">Stopped</span>}
        {tile.running === null ? (
          tile.subtitle && <Clip className="flex-1 text-[11px] text-muted">{tile.subtitle}</Clip>
        ) : (
          <Clip className="flex-1 text-right font-mono text-[11px] text-muted">{tile.node}</Clip>
        )}
      </div>
      {showBars && (
        <div className="mt-1 grid grid-cols-2 gap-2">
          {cpuV !== undefined && <Bar label="CPU" value={cpuV} color={color} />}
          {memV !== undefined && <Bar label="MEM" value={memV} color={color} />}
        </div>
      )}
    </div>
  );
}

function Columns({
  frame,
  tiles,
  color,
  cpu = EMPTY,
  mem = EMPTY,
}: {
  frame: MapFrame;
  tiles: GuestTile[];
  color: string;
  cpu?: Usage;
  mem?: Usage;
}) {
  const columns = guestColumns(tiles, frame.groups ?? []);
  if (columns.length === 0) return null;
  const untitled = columns.length === 1 && columns[0]?.title === "";
  return (
    <div
      data-guest-groups=""
      className="grid min-w-0 flex-1 items-start gap-4"
      style={{
        gridTemplateColumns: untitled
          ? "repeat(auto-fill, minmax(170px, 1fr))"
          : `repeat(${columns.length}, minmax(0, 1fr))`,
      }}
    >
      {columns.map((c) => (
        <div
          key={c.id}
          data-group={c.id}
          className={untitled ? "contents" : "flex min-w-0 flex-col gap-2"}
        >
          {c.title && (
            <Clip className="text-[10px] font-bold tracking-wide text-muted uppercase">
              {c.title}
            </Clip>
          )}
          {c.tiles.map((t) => (
            <Tile
              key={t.key}
              tile={t}
              bars={frame.bars !== false}
              color={color}
              cpu={cpu}
              mem={mem}
            />
          ))}
        </div>
      ))}
    </div>
  );
}

function ProxmoxColumns({
  frame,
  instanceId,
  color,
}: {
  frame: MapFrame;
  instanceId: string;
  color: string;
}) {
  // The same combined queries the guests card uses: one per metric, never one per guest.
  const { guests, cpuBy, memBy } = useProxmoxGuests(instanceId, {
    bars: frame.bars !== false,
  });
  return (
    <Columns
      frame={frame}
      tiles={guestTiles(guests, frame.guests)}
      color={color}
      cpu={frame.bars !== false ? cpuBy : EMPTY}
      mem={frame.bars !== false ? memBy : EMPTY}
    />
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
  const ref = useReportHeight(frame.id);
  const instance = live.instance(frame.instance_id);
  const state = live.state(frame.instance_id);
  const down = state === "down";
  const removed = state === "removed";
  const color = paint(frame.color) ?? deviceColor(instance);
  const name = frame.label || instance?.name || "";
  const box = sideBox(frame);
  const m: BoxModel = {
    box,
    name,
    state,
    down,
    removed,
    linked: frame.instance_id !== null,
    instance,
    color,
    identity: boxIdentity(box, instance, live),
    live,
    vlans: new Map(),
    rate: null,
    cabled: new Map(),
    select: {},
  };
  const sideWidth = Math.max(200, Math.min(300, Math.round(frame.w * 0.22)));
  const bar = color ?? "var(--vt-in)";
  return (
    <div
      ref={ref}
      data-element={frame.id}
      data-kind="frame"
      data-down={down ? "true" : undefined}
      className={`absolute box-border rounded-xl border px-4 pt-6 pb-4 focus-visible:ring-2 focus-visible:ring-in focus-visible:outline-none ${down ? "border-crit" : "border-line"} ${instance && onSelect ? "cursor-pointer" : ""}`}
      {...selectable(frame.id, instance !== undefined && !removed, onSelect, name)}
      style={{
        left: frame.x,
        top: frame.y,
        width: frame.w,
        height: frameSized(frame) ? undefined : frame.h,
        borderTop: `3px solid ${down ? "var(--vt-crit)" : (color ?? "var(--vt-line)")}`,
        background: color ? tint(color, 4) : undefined,
      }}
    >
      <div
        data-tab=""
        className="absolute -top-[18px] left-3.5 z-[1] flex max-w-[calc(100%-28px)] items-baseline gap-1.5 rounded-t-lg border border-b-0 border-line bg-panel px-2.5 py-0.5 whitespace-nowrap"
      >
        <b className={`min-w-0 truncate text-[15px] ${down ? "text-crit-text" : ""}`} title={name}>
          {name}
        </b>
        {instance && (
          <span className="min-w-0 truncate text-[12px] text-muted">
            · {live.typeName(instance.type_id)}
          </span>
        )}
        <StatusWord state={state} />
      </div>
      <div className="flex min-w-0 items-start gap-5">
        {frame.side && !removed && (
          <div
            data-side=""
            className="flex shrink-0 flex-col gap-2.5 overflow-hidden"
            style={{ width: sideWidth }}
          >
            <IdentityBlock m={m} />
            <SectionsView m={m} />
          </div>
        )}
        {instance?.type_id === "proxmox" ? (
          <ProxmoxColumns frame={frame} instanceId={instance.id} color={bar} />
        ) : (
          frame.guests.manual.length > 0 && (
            <Columns frame={frame} tiles={guestTiles([], frame.guests)} color={bar} />
          )
        )}
      </div>
      {removed && <div className="text-[12px] text-muted">Device removed</div>}
    </div>
  );
}
