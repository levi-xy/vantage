// A device box, drawn read-only in the shape the admin picked. The width is the stored `w`;
// the height comes from the content (the stored `h` is ignored), and the box reports its
// rendered height so cables meet its real edges. Every string is rendered as React text.

import type { ReactNode } from "react";
import type { MapBox, MapVlan } from "../../api";
import { deviceColor } from "../../app/deviceColors";
import { useUnits } from "../../app/units";
import { boxIdentity, paint, selectable } from "./boxFacts";
import { BoxRoot, FigureLine, IdentityBlock, NameTab, SectionsView } from "./boxParts";
import type { BoxModel } from "./boxParts";
import type { MapLive } from "./mapLive";
import { NasBays } from "./NasBays";
import { PanelView } from "./PanelView";
import { boxPadX, panelGeometry, panelRoom } from "./panelGeometry";

/** The panel of a box, ready to draw (null without ports, or for a removed device). */
function panelOf(m: BoxModel) {
  if (m.removed || !m.box.panel) return null;
  return panelGeometry(m.box.panel, m.live.ports(m.box.instance_id), panelRoom(m.box));
}

/** Identity, figures and sections, in the padding every shape shares. */
function Content({
  m,
  padX = "px-3.5",
  top = "pt-6",
}: {
  m: BoxModel;
  padX?: string;
  top?: string;
}) {
  return (
    <div className={`flex min-w-0 flex-col gap-2.5 overflow-hidden pb-3 ${top} ${padX}`}>
      <IdentityBlock m={m} />
      <FigureLine m={m} />
      <SectionsView m={m} />
    </div>
  );
}

/**
 * The content of a box with its front panel in the inset strip: below the content, above it,
 * or in a column beside it. `lead` is a column before the content (a NAS's drive bays).
 */
function Body({ m, padX, lead }: { m: BoxModel; padX?: string; lead?: ReactNode }) {
  const geometry = panelOf(m);
  const place = m.box.panel?.place ?? "bottom";
  const pad = boxPadX(m.box);
  const panel = geometry ? <PanelView m={m} geometry={geometry} /> : null;
  return (
    <>
      {place === "top" && panel && (
        <div className="flex" style={{ margin: `12px ${pad}px 0` }}>
          {panel}
        </div>
      )}
      <div className="flex min-w-0 items-start">
        {lead}
        <div className="min-w-0 flex-1">
          <Content m={m} padX={padX} top={place === "top" && panel ? "pt-3" : undefined} />
        </div>
        {place === "side" && panel && (
          <div className="shrink-0" style={{ margin: `24px ${pad}px 12px 0` }}>
            {panel}
          </div>
        )}
      </div>
      {place === "bottom" && panel && (
        <div className="flex" style={{ margin: `0 ${pad}px 12px` }}>
          {panel}
        </div>
      )}
    </>
  );
}

const topBar = (m: BoxModel) =>
  m.color && m.instance && !m.removed
    ? { borderTop: `3px solid ${m.down ? "var(--vt-crit)" : m.color}` }
    : undefined;

const FACEPLATE = {
  background:
    "linear-gradient(180deg, var(--vt-chip) 0%, var(--vt-panel) 55%, var(--vt-inset) 100%)",
  boxShadow: "inset 0 1px 0 color-mix(in srgb, var(--vt-ink) 8%, transparent)",
} as const;

function CardBox({ m }: { m: BoxModel }) {
  return (
    <BoxRoot m={m} className="rounded-[10px] border bg-panel" style={topBar(m)}>
      <NameTab m={m} />
      <Body m={m} />
    </BoxRoot>
  );
}

function RackEar({ side }: { side: "left" | "right" }) {
  return (
    <span
      aria-hidden="true"
      data-ear={side}
      className={`absolute inset-y-0 flex w-2 flex-col items-center justify-between bg-chip py-1.5 ${
        side === "left" ? "left-0 border-r" : "right-0 border-l"
      } border-line`}
    >
      <span className="block h-2.5 w-[3px] rounded-full bg-inset" />
      <span className="block h-2.5 w-[3px] rounded-full bg-inset" />
    </span>
  );
}

/** Metal faceplate; `rack` adds ears with screw slots, `desktop` adds two feet, `flat` neither. */
function DeviceBox({ m, look }: { m: BoxModel; look: "rack" | "desktop" | "flat" }) {
  return (
    <BoxRoot m={m} className="rounded-lg border" style={{ ...FACEPLATE, ...topBar(m) }}>
      {look === "rack" && (
        <>
          <RackEar side="left" />
          <RackEar side="right" />
        </>
      )}
      <NameTab m={m} />
      <Body m={m} padX={look === "rack" ? "px-5" : "px-3.5"} />
      {look === "desktop" && (
        <>
          <span
            aria-hidden="true"
            data-foot=""
            className="absolute -bottom-1 left-8 h-1 w-10 rounded-b-sm bg-line"
          />
          <span
            aria-hidden="true"
            data-foot=""
            className="absolute right-8 -bottom-1 h-1 w-10 rounded-b-sm bg-line"
          />
        </>
      )}
    </BoxRoot>
  );
}

/** A rounded puck: large top corners. */
function ApBox({ m }: { m: BoxModel }) {
  return (
    <BoxRoot m={m} className="rounded-t-[40px] rounded-b-[14px] border bg-panel" style={topBar(m)}>
      <NameTab m={m} />
      <Body m={m} padX="px-5" />
    </BoxRoot>
  );
}

/** A NAS: faceplate with the drive bays on the left, identity and sections on the right. */
function NasBox({ m }: { m: BoxModel }) {
  return (
    <BoxRoot m={m} className="rounded-lg border" style={{ ...FACEPLATE, ...topBar(m) }}>
      <NameTab m={m} />
      <Body m={m} lead={<NasBays m={m} />} />
    </BoxRoot>
  );
}

function RateInline({ rate }: { rate: NonNullable<BoxModel["rate"]> }) {
  const { formatRate } = useUnits();
  return (
    <span data-rate="" className="text-[11px] tabular-nums">
      <b className="text-in">↓ {rate.inBps === undefined ? "–" : formatRate(rate.inBps)}</b>{" "}
      <b className="text-out">↑ {rate.outBps === undefined ? "–" : formatRate(rate.outBps)}</b>
    </span>
  );
}

const CLOUD = [
  "M50 102 C24 102 8 88 8 70 C8 54 20 44 36 42 C38 22 58 8 82 8",
  "C100 8 114 16 122 28 C128 24 136 22 144 22 C164 22 178 36 180 52",
  "C194 54 202 66 202 78 C202 92 190 102 174 102 Z",
].join(" ");

/** A cloud outline scaled to the box (its height follows its width); no tab. */
function CloudBox({ m }: { m: BoxModel }) {
  const { box, name, down } = m;
  const height = Math.max(80, Math.round(box.w * 0.55));
  return (
    <BoxRoot m={m} scalable={false} className="flex items-center justify-center" style={{ height }}>
      <svg
        aria-hidden="true"
        viewBox="0 0 210 110"
        preserveAspectRatio="none"
        className="absolute inset-0 h-full w-full overflow-visible"
      >
        <path
          d={CLOUD}
          fill="var(--vt-panel)"
          stroke={down ? "var(--vt-crit)" : "var(--vt-line)"}
          strokeWidth="1.5"
          vectorEffect="non-scaling-stroke"
        />
      </svg>
      <span className="relative flex min-w-0 max-w-[60%] flex-col items-center pt-3 text-center">
        <b
          className={`block max-w-full truncate text-[17px] ${down ? "text-crit-text" : ""}`}
          title={name}
        >
          {name}
        </b>
        {m.rate ? (
          <RateInline rate={m.rate} />
        ) : (
          box.subtitle && (
            <span className="block max-w-full truncate text-[11px] text-muted" title={box.subtitle}>
              {box.subtitle}
            </span>
          )
        )}
      </span>
    </BoxRoot>
  );
}

const NO_CABLES: BoxModel["cabled"] = new Map();

export function BoxView({
  box,
  live,
  vlans,
  onSelect,
  rate = null,
  cabled = NO_CABLES,
}: {
  box: MapBox;
  live: MapLive;
  vlans: ReadonlyMap<string, MapVlan>;
  onSelect?: (id: string) => void;
  /** Live in/out of the cables on this box, for the cloud shape. */
  rate?: BoxModel["rate"];
  /** Port id to the name of the box at the other end of its cable (captions from cables). */
  cabled?: BoxModel["cabled"];
}) {
  const instance = live.instance(box.instance_id);
  const state = live.state(box.instance_id);
  const removed = state === "removed";
  const name = box.label || instance?.name || "";
  const m: BoxModel = {
    box,
    name: removed ? box.label || "Device" : name,
    state,
    down: state === "down",
    removed,
    linked: box.instance_id !== null,
    instance,
    color: paint(box.color) ?? deviceColor(instance),
    identity: boxIdentity(box, instance, live),
    live,
    vlans,
    rate,
    cabled,
    select: selectable(box.id, instance !== undefined && !removed, onSelect, name),
  };
  switch (box.shape ?? "card") {
    case "cloud":
      return <CloudBox m={m} />;
    case "device":
      return <DeviceBox m={m} look={box.panel?.look ?? "flat"} />;
    case "nas":
      return <NasBox m={m} />;
    case "ap":
      return <ApBox m={m} />;
    default:
      return <CardBox m={m} />;
  }
}
