import type { MapVlan } from "../../api";
import { colorVar, isDeviceColor } from "../../app/deviceColors";
import { NOTCH, STATE_TEXT, TILE } from "../../cards/snmp-ports/ports";
import { BAR_H, JACK_H } from "./strip";
import type { Jack, StripLayout } from "./strip";

const NEUTRAL = "border-line bg-inset text-muted";

function JackView({ jack, vlans }: { jack: Jack; vlans: ReadonlyMap<string, MapVlan> }) {
  const { port } = jack;
  const state = port.state;
  const look = state ? TILE[state] : NEUTRAL;
  const notch = state ? NOTCH[state] : "bg-port-off";
  const stateText = state ? STATE_TEXT[state] || "link up" : "";
  const bars = [
    ...(port.vlans.untagged ? [{ id: port.vlans.untagged, h: 8 }] : []),
    ...port.vlans.tagged.map((id) => ({ id, h: 4 })),
  ];
  return (
    <span
      data-port={port.id}
      data-state={state ?? undefined}
      title={[port.tip, stateText].filter(Boolean).join(" · ")}
      className="absolute block"
      style={{ left: jack.x, top: jack.y, width: jack.w, height: JACK_H + BAR_H }}
    >
      <span
        className={`relative box-border flex items-center justify-center overflow-hidden border text-[8px] leading-none ${look} ${
          port.kind === "sfp" ? "rounded-[2px]" : "rounded-[3px]"
        }`}
        style={{ width: jack.w, height: JACK_H }}
      >
        {port.kind === "sfp" ? (
          <span
            aria-hidden="true"
            className="absolute inset-[3px] rounded-[1px] border border-line bg-canvas"
          />
        ) : (
          <span
            aria-hidden="true"
            className={`absolute top-0 left-1/2 h-[2px] w-1/2 -translate-x-1/2 ${notch}`}
          />
        )}
        {port.label && <span className="relative">{port.label}</span>}
      </span>
      <span className="flex items-start gap-px pt-px" aria-hidden="true">
        {bars.map((b, i) => {
          const color = vlans.get(b.id)?.color;
          return (
            <span
              key={`${b.id}-${i}`}
              data-vlan={b.id}
              className="block w-[3px] rounded-b-[1px]"
              style={{
                height: b.h,
                background: isDeviceColor(color) ? colorVar(color) : "var(--vt-muted)",
              }}
            />
          );
        })}
      </span>
    </span>
  );
}

/** The jacks of a box, along its bottom edge. The drawing is the cables' anchor too. */
export function PortStrip({
  layout,
  origin,
  vlans,
}: {
  layout: StripLayout;
  origin: { x: number; y: number };
  vlans: ReadonlyMap<string, MapVlan>;
}) {
  return (
    <div
      data-strip=""
      className="absolute"
      style={{ left: origin.x, top: origin.y, width: layout.width, height: layout.height }}
    >
      {layout.jacks.map((j) => (
        <JackView key={j.port.id} jack={j} vlans={vlans} />
      ))}
    </div>
  );
}
