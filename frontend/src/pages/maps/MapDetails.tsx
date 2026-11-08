// The two pop-ups of the live map: the card that follows the pointer over a cable, and the
// details drawer of a clicked device. Range queries run only while one of them is open.

import { useEffect } from "react";
import { Link } from "react-router";
import type { MapBox, MapDocument, MapFrame } from "../../api";
import { useUnits } from "../../app/units";
import { useProxmoxGuests } from "../../cards/proxmox-guests/useProxmoxGuests";
import { AreaChart } from "../../cards/metrics/Charts";
import { parseValue } from "../../cards/metrics/promql";
import { useFleetRangeQuery } from "../../cards/metrics/useMetricQuery";
import { historyQuery } from "../../cards/throughput/ports";
import type { PortRef } from "../../cards/throughput/ports";
import { cableLive, endElements } from "./cableLive";
import type { CableLive } from "./cableLive";
import { boxFigures } from "./figures";
import { speedLabel } from "./geometry";
import { guestTiles } from "./guests";
import { StatusWord } from "./MapElements";
import { instanceAddress } from "./mapLive";
import type { MapLive } from "./mapLive";

const HOUR = 3600;
const EMPTY_GUESTS = { auto: true, hidden: [], order: [], manual: [] };

function RateLine({ rate }: { rate: NonNullable<CableLive["rate"]> }) {
  const { formatRate } = useUnits();
  return (
    <span className="tabular-nums">
      <span className="text-in">↓ {formatRate(rate.inBps)}</span>{" "}
      <span className="text-out">↑ {formatRate(rate.outBps)}</span>
    </span>
  );
}

/** In and out of a port over the last hour (the throughput card's queries, for one port). */
function PortChart({ port }: { port: PortRef }) {
  const { unit, formatRate } = useUnits();
  const rx = useFleetRangeQuery(historyQuery([port], "in"), HOUR);
  const tx = useFleetRangeQuery(historyQuery([port], "out"), HOUR);
  const points = (r: typeof rx.data) =>
    (r?.series[0]?.values ?? []).flatMap(([t, raw]) => {
      const v = parseValue(raw);
      return v === undefined ? [] : [[t, v] as [number, number]];
    });
  return (
    <div className="h-24">
      <AreaChart
        label="Port traffic, last hour"
        format={formatRate}
        tickScale={unit === "bytes" ? 8 : 1}
        series={[
          { name: "In", points: points(rx.data), tone: "in", area: true },
          { name: "Out", points: points(tx.data), tone: "out" },
        ]}
      />
    </div>
  );
}

/** Both ends, speed, state, the current rate and the last hour. */
export function CableCard({
  cableId,
  doc,
  live,
}: {
  cableId: string;
  doc: MapDocument;
  live: MapLive;
}) {
  const cable = doc.elements.find((e) => e.kind === "cable" && e.id === cableId);
  if (cable?.kind !== "cable") return null;
  const state = cableLive(cable, endElements(doc), live);
  const end = (e: CableLive["ends"][number]) => [e.device, e.port].filter(Boolean).join(" · ");
  return (
    <div
      role="tooltip"
      className="pointer-events-none w-64 rounded-[10px] border border-line bg-panel p-3 text-[13px] text-ink shadow-lg"
    >
      <div className="font-medium">{end(state.ends[0])}</div>
      <div className="text-muted">to</div>
      <div className="font-medium">{end(state.ends[1])}</div>
      <div className="mt-1.5 flex items-center gap-2 text-muted">
        {cable.speed && <span>{speedLabel(cable.speed)}</span>}
        <b className={state.down ? "text-crit-text" : "text-ok-text"}>
          {state.down ? "DOWN" : "UP"}
        </b>
        {state.rate && <RateLine rate={state.rate} />}
      </div>
      {state.rate && (
        <div className="mt-2">
          <PortChart port={state.rate.ref} />
        </div>
      )}
    </div>
  );
}

/** The device's main figure over the last hour: one fleet query, this device's series. */
function MainChart({
  instanceId,
  box,
  typeId,
}: {
  instanceId: string;
  box: Pick<MapBox, "figures">;
  typeId: string;
}) {
  const def = boxFigures(box, typeId)[0];
  const range = useFleetRangeQuery(def?.expr ?? "vector(0)", HOUR, def !== undefined);
  if (!def) return null;
  const series = range.data?.series.find((s) => s.labels.vantage_instance_id === instanceId);
  const points = (series?.values ?? []).flatMap(([t, raw]) => {
    const v = parseValue(raw);
    return v === undefined ? [] : [[t, v] as [number, number]];
  });
  return (
    <section>
      <h3 className="mb-1 text-xs font-bold tracking-wide text-muted uppercase">
        {def.label}, last hour
      </h3>
      <div className="h-24">
        <AreaChart
          label={`${def.label}, last hour`}
          format={def.format}
          series={[{ name: def.label, points, tone: "line", area: true }]}
        />
      </div>
    </section>
  );
}

function Guests({ instanceId, frame }: { instanceId: string; frame: MapFrame }) {
  const { guests } = useProxmoxGuests(instanceId);
  const tiles = guestTiles(guests, frame.guests ?? EMPTY_GUESTS).slice(0, 30);
  if (tiles.length === 0) return null;
  return (
    <section>
      <h3 className="mb-1 text-xs font-bold tracking-wide text-muted uppercase">Guests</h3>
      <ul className="text-[13px]">
        {tiles.map((t) => (
          <li key={t.key} className="flex justify-between gap-2 py-0.5">
            <span className="truncate">{t.name}</span>
            <span className={t.running === false ? "text-muted" : "text-ok-text"}>
              {t.running === true ? "UP" : t.running === false ? "Stopped" : ""}
            </span>
          </li>
        ))}
      </ul>
    </section>
  );
}

/** A linked box or frame: state, figures, chart, cables, guests and a link to its page. */
export function DetailsDrawer({
  elementId,
  doc,
  live,
  onClose,
}: {
  elementId: string;
  doc: MapDocument;
  live: MapLive;
  onClose: () => void;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const owners = endElements(doc);
  const el = owners.get(elementId);
  const instance = live.instance(el?.instance_id ?? null);
  if (!el || !instance) return null;
  const state = live.state(instance.id);
  const name = el.label || instance.name;
  const box = el.kind === "box" ? el : { figures: [] };
  const figures = live.figures(box, instance);
  const cables = doc.elements.flatMap((c) => {
    if (c.kind !== "cable") return [];
    const mine = c.from.element === elementId ? 0 : c.to.element === elementId ? 1 : -1;
    return mine < 0
      ? []
      : [{ cable: c, other: cableLive(c, owners, live).ends[mine === 0 ? 1 : 0] }];
  });
  return (
    <aside
      aria-label={`Details of ${name}`}
      className="absolute top-0 right-0 bottom-0 z-10 flex w-80 max-w-full cursor-default flex-col gap-4 overflow-y-auto border-l border-line bg-panel p-4 text-ink"
      onPointerDown={(e) => e.stopPropagation()}
      onWheel={(e) => e.stopPropagation()}
    >
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <h2 className="truncate text-base font-semibold">{name}</h2>
          <StatusWord state={state} />
          <div className="truncate font-mono text-[12px] text-muted">
            {[live.typeName(instance.type_id), instanceAddress(instance)]
              .filter(Boolean)
              .join(" · ")}
          </div>
        </div>
        <button
          type="button"
          aria-label="Close details"
          className="rounded px-2 text-xl leading-none text-muted hover:text-ink focus-visible:ring-2 focus-visible:ring-in focus-visible:outline-none"
          onClick={onClose}
        >
          ×
        </button>
      </div>
      {figures.length > 0 && (
        <dl className="grid grid-cols-3 gap-2">
          {figures.map(({ def, text }) => (
            <div key={def.id} className="rounded-lg bg-inset px-2 py-1.5">
              <dt className="truncate text-[11px] text-muted">{def.label}</dt>
              <dd className="truncate text-[15px] font-medium tabular-nums">{text}</dd>
            </div>
          ))}
        </dl>
      )}
      <MainChart instanceId={instance.id} box={box} typeId={instance.type_id} />
      {cables.length > 0 && (
        <section>
          <h3 className="mb-1 text-xs font-bold tracking-wide text-muted uppercase">Cables</h3>
          <ul className="text-[13px]">
            {cables.map(({ cable, other }) => {
              const rate = cableLive(cable, owners, live).rate;
              return (
                <li key={cable.id} className="py-0.5">
                  {[other.device, other.port, cable.speed ? speedLabel(cable.speed) : ""]
                    .filter(Boolean)
                    .join(" · ")}{" "}
                  {rate && <RateLine rate={rate} />}
                </li>
              );
            })}
          </ul>
        </section>
      )}
      {el.kind === "frame" && instance.type_id === "proxmox" && (
        <Guests instanceId={instance.id} frame={el} />
      )}
      <Link
        to={`/devices/${instance.id}`}
        className="mt-auto rounded-lg border border-line px-3 py-2 text-center text-sm font-medium text-ink hover:bg-inset focus-visible:ring-2 focus-visible:ring-in focus-visible:outline-none"
      >
        Open device page
      </Link>
    </aside>
  );
}
