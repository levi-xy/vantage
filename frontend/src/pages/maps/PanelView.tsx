// A box's front panel, drawn read-only: RJ45 jacks and SFP cages with link and activity LEDs,
// the number above (below on an upside-down jack), VLAN bars and the caption on the other
// side, and a hover card per port. Every cell sits at the numbers of `panelGeometry`, so the
// cables (which add the panel's measured origin) meet the real jacks. Plain text only.

import { useState } from "react";
import type { MapVlan } from "../../api";
import { colorVar, isDeviceColor } from "../../app/deviceColors";
import { useUnits } from "../../app/units";
import { formatBitsPerSecond } from "../../cards/metrics/format";
import { STATE_TEXT } from "../../cards/snmp-ports/ports";
import type { PortState } from "../../cards/snmp-ports/ports";
import { Clip } from "./boxParts";
import type { BoxModel } from "./boxParts";
import type { DevicePort } from "./devicePorts";
import { KIND_LABEL, portCaption } from "./panel";
import { INSET, panelOuter } from "./panelGeometry";
import type { Jack, PanelGeometry } from "./panelGeometry";
import { useReportPanelOrigin } from "./measure";

// The physical parts look the same in light and dark mode: a dark opening, gold contacts.
const OPENING = "#12161c";
const GOLD = "#c9a227";
const LED_OFF = "var(--vt-port-off)";

interface Leds {
  link: string;
  activity: string;
}

/**
 * Link LED: green = up, red = down on a port that has a cable (a port nobody uses is not an
 * alarm), dark = unused, disabled or not reported. Activity LED: lit while traffic flows.
 */
function leds(device: DevicePort | undefined, cabled: boolean): Leds {
  const state: PortState | null = device?.state ?? null;
  const traffic = (device?.inBps ?? 0) + (device?.outBps ?? 0) > 0;
  return {
    link: state === "up" ? "var(--vt-ok)" : state === "down" && cabled ? "var(--vt-crit)" : LED_OFF,
    activity: state === "up" && traffic ? "var(--vt-out)" : LED_OFF,
  };
}

function JackDrawing({ jack, stroke, led }: { jack: Jack; stroke: string; led: Leds }) {
  const { shape, flip, jackW: w, jackH: h } = jack;
  const kind = jack.group.kind;
  const turn = (c: React.ReactNode, vw: number, vh: number) =>
    flip ? <g transform={`rotate(180 ${vw / 2} ${vh / 2})`}>{c}</g> : <g>{c}</g>;
  const svg = (vw: number, vh: number, inner: React.ReactNode) => (
    <svg
      aria-hidden="true"
      width={w}
      height={h}
      viewBox={`0 0 ${vw} ${vh}`}
      preserveAspectRatio="none"
      style={{ display: "block" }}
    >
      <rect
        x="0.75"
        y="0.75"
        width={vw - 1.5}
        height={vh - 1.5}
        rx="2"
        fill="var(--vt-chip)"
        stroke={stroke}
        strokeWidth="1.5"
      />
      {turn(inner, vw, vh)}
    </svg>
  );
  if (shape === "rj45") {
    return svg(
      26,
      22,
      <>
        <path d="M5 4 H21 V14 H17 V18 H9 V14 H5 Z" fill={OPENING} />
        {Array.from({ length: 8 }, (_, i) => (
          <rect key={i} x={6.6 + i * 1.65} y="4.6" width="0.9" height="3" fill={GOLD} />
        ))}
        <rect x="2" y="17" width="3.5" height="2.2" rx=".5" fill={led.link} />
        <rect x="20.5" y="17" width="3.5" height="2.2" rx=".5" fill={led.activity} />
      </>,
    );
  }
  if (shape === "cage") {
    const [vw, vh] = kind === "qsfp" ? [40, 20] : [34, 18];
    return svg(
      vw,
      vh,
      <>
        <rect x="4" y="5" width={vw - 8} height="8" fill={OPENING} />
        <rect x="4" y="2.5" width={vw - 8} height="1.2" fill="var(--vt-port-off)" />
        <circle cx={vw - 3.5} cy={vh - 3} r="1.3" fill={led.link} />
      </>,
    );
  }
  if (shape === "console") {
    return svg(
      26,
      16,
      <>
        <rect x="5" y="3" width="16" height="8" rx="1" fill={OPENING} />
        <rect x="8" y="11" width="10" height="2" fill={GOLD} />
      </>,
    );
  }
  return svg(
    18,
    12,
    <>
      <rect x="4" y="3" width="10" height="6" fill={OPENING} />
      <rect x="6" y="5" width="6" height="1.5" fill={GOLD} />
    </>,
  );
}

function VlanBars({ jack, vlans }: { jack: Jack; vlans: ReadonlyMap<string, MapVlan> }) {
  const bars = [
    ...(jack.port.vlans.untagged ? [{ id: jack.port.vlans.untagged, h: 7 }] : []),
    ...jack.port.vlans.tagged.map((id) => ({ id, h: 4 })),
  ];
  return (
    <span
      aria-hidden="true"
      className="absolute inset-x-0 flex items-end justify-center gap-px"
      style={{ top: jack.barsY, height: 8 }}
    >
      {bars.map((b, i) => {
        const color = vlans.get(b.id)?.color;
        return (
          <span
            key={`${b.id}-${i}`}
            data-vlan={b.id}
            className="block w-[4px]"
            style={{
              height: b.h,
              background: isDeviceColor(color) ? colorVar(color) : "var(--vt-muted)",
            }}
          />
        );
      })}
    </span>
  );
}

function HoverCard({
  jack,
  device,
  caption,
  fromCable,
  vlans,
  right,
}: {
  jack: Jack;
  device: DevicePort | undefined;
  caption: string;
  right: boolean;
  fromCable: boolean;
  vlans: ReadonlyMap<string, MapVlan>;
}) {
  const { formatRate } = useUnits();
  const { port, number } = jack;
  const iface = port.interface || port.id;
  const state = device?.state ?? null;
  const vlanText = (id: string) => {
    const v = vlans.get(id);
    return v ? `${id} · ${v.name}` : id;
  };
  return (
    <div
      role="tooltip"
      data-port-card={port.id}
      className="pointer-events-none absolute z-30 w-56 rounded-lg border border-line bg-panel p-2.5 text-[12px] leading-[17px] text-ink shadow-lg"
      style={{ ...(right ? { right: 0 } : { left: 0 }), top: "100%", marginTop: 6 }}
    >
      <span className="block truncate font-bold">
        {KIND_LABEL[jack.group.kind]} {number}
        {device && <span className="font-mono text-[11px] font-normal text-muted"> · {iface}</span>}
        {state && (
          <span className={state === "up" ? "text-ok-text" : "text-muted"}>
            {" · "}
            {state === "up" ? "UP" : STATE_TEXT[state]}
            {state === "up" && device?.speed ? ` · ${formatBitsPerSecond(device.speed)}` : ""}
          </span>
        )}
      </span>
      {caption && (
        <span className="block truncate">
          Caption <b>{caption}</b>
          {fromCable && <span className="text-muted"> (from the cable)</span>}
        </span>
      )}
      <span className="block truncate text-muted">
        {port.vlans.untagged ? `Untagged ${vlanText(port.vlans.untagged)}` : "No untagged VLAN"}
        {port.vlans.tagged.length > 0 && ` · tagged ${port.vlans.tagged.join(", ")}`}
      </span>
      {device && (device.inBps !== undefined || device.outBps !== undefined) && (
        <span className="block tabular-nums">
          <b className="text-in">↓ {device.inBps === undefined ? "–" : formatRate(device.inBps)}</b>{" "}
          <b className="text-out">
            ↑ {device.outBps === undefined ? "–" : formatRate(device.outBps)}
          </b>
        </span>
      )}
    </div>
  );
}

/** The panel in its inset strip. */
export function PanelView({ m, geometry }: { m: BoxModel; geometry: PanelGeometry }) {
  const { box, live, vlans } = m;
  const panel = box.panel;
  const [tip, setTip] = useState<string | null>(null);
  const origin = useReportPanelOrigin(box.id);
  const outer = panelOuter(geometry);
  if (!panel) return null;
  const { flags } = geometry;
  return (
    <div
      data-panel=""
      data-look={panel.look}
      className="relative box-border shrink-0 rounded-md border border-line bg-inset"
      style={{ width: outer.w, height: outer.h }}
    >
      <div
        ref={origin}
        className="absolute"
        style={{ left: INSET - 1, top: INSET - 1, width: geometry.width, height: geometry.height }}
      >
        {geometry.jacks.map((jack) => {
          const { port } = jack;
          const device = live.port(box.instance_id, port.interface || port.id);
          const fromCable = m.cabled.get(port.id);
          const caption = portCaption(panel.captions, port.caption, fromCable);
          const state = device?.state ?? undefined;
          const tone = vlans.get(port.vlans.untagged ?? "")?.color;
          const stroke = isDeviceColor(tone) ? colorVar(tone) : "var(--vt-port-off)";
          return (
            <span
              key={port.id}
              data-port={port.id}
              data-state={state}
              data-jack={jack.shape}
              data-flip={jack.flip ? "true" : undefined}
              role="img"
              aria-label={`${KIND_LABEL[jack.group.kind]} ${jack.number}${caption ? ` ${caption}` : ""}`}
              tabIndex={0}
              className="absolute block focus-visible:outline-none"
              style={{
                left: jack.x,
                top: jack.y,
                width: jack.w,
                height: jack.h,
                zIndex: tip === port.id ? 30 : undefined,
              }}
              onMouseEnter={() => setTip(port.id)}
              onMouseLeave={() => setTip(null)}
              onFocus={() => setTip(port.id)}
              onBlur={() => setTip(null)}
            >
              {flags.numbers && (
                <span
                  data-number=""
                  className="absolute inset-x-0 block text-center text-[9px] leading-[11px] text-muted"
                  style={{ top: jack.numberY }}
                >
                  {jack.number}
                </span>
              )}
              <span
                className="absolute block"
                style={{ left: jack.jackX, top: jack.jackY, width: jack.jackW, height: jack.jackH }}
              >
                <JackDrawing
                  jack={jack}
                  stroke={stroke}
                  led={leds(device, fromCable !== undefined)}
                />
              </span>
              {flags.bars && <VlanBars jack={jack} vlans={vlans} />}
              {flags.captions && (
                <Clip
                  className="absolute inset-x-0 text-center text-[8.5px] leading-[11px] text-muted"
                  style={{ top: jack.captionY }}
                >
                  {caption}
                </Clip>
              )}
              {tip === port.id && (
                <HoverCard
                  jack={jack}
                  device={device}
                  caption={caption}
                  fromCable={panel.captions === "cables" && fromCable !== undefined}
                  vlans={vlans}
                  right={jack.x > geometry.width / 2}
                />
              )}
            </span>
          );
        })}
      </div>
    </div>
  );
}
