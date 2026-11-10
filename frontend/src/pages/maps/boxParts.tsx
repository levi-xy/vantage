// The pieces every box shape is made of: the root (measured, selectable), the name tab on the
// top edge, the identity block and the sections. Every string comes from the saved document or
// from device names, so it is only ever rendered as React text. Long text is cut to one line
// with "…" and the full text is the hover title: nothing overflows its box.

import { useRef } from "react";
import type { CSSProperties, ReactNode } from "react";
import type { Instance, MapBox, MapVlan } from "../../api";
import { tint } from "../../app/deviceColors";
import { paint, shownItems } from "./boxFacts";
import { useFitScale } from "./fitScale";
import type { Identity } from "./boxFacts";
import { useReportHeight } from "./measure";
import { MapIcon } from "./MapIcon";
import type { DeviceState, MapLive } from "./mapLive";

export function StatusWord({ state }: { state: string | null }) {
  if (state === "up") {
    return <b className="text-[12px] font-bold text-ok-text">UP</b>;
  }
  if (state === "down") {
    return <b className="text-[12px] font-bold text-crit-text">DOWN</b>;
  }
  return null;
}

/** One line of text, cut with "…", the whole text on hover. */
export function Clip({
  children,
  className = "",
  style,
}: {
  children: string;
  className?: string;
  style?: CSSProperties;
}) {
  return (
    <span className={`block min-w-0 truncate ${className}`} style={style} title={children}>
      {children}
    </span>
  );
}

/** Everything a shape needs to draw a box, worked out once by `BoxView`. */
export interface BoxModel {
  box: MapBox;
  /** The label, else the device's name. */
  name: string;
  state: DeviceState | null;
  down: boolean;
  removed: boolean;
  linked: boolean;
  instance: Instance | undefined;
  /** The box colour (own or the device's), as a CSS value. */
  color: string | undefined;
  identity: Identity;
  live: MapLive;
  vlans: ReadonlyMap<string, MapVlan>;
  /** Live in/out of the cables on this box (the cloud shows it), bits per second. */
  rate: { inBps?: number; outBps?: number } | null;
  /** Port id to the name at the other end of its cable: the ports that have a cable. */
  cabled: ReadonlyMap<string, string>;
  /** Opens the details drawer (only for a linked, existing device). */
  select: Record<string, unknown>;
}

/** The root element of a box: positioned at x/y, `w` wide, as tall as its content. */
export function BoxRoot({
  m,
  className,
  style,
  children,
  scalable = true,
}: {
  m: BoxModel;
  className: string;
  style?: CSSProperties;
  children: ReactNode;
  /** Content scales up when the box is taller than it (not for the cloud, which sizes itself). */
  scalable?: boolean;
}) {
  const { box, down, instance } = m;
  const ref = useReportHeight(box.id);
  const inner = useRef<HTMLDivElement>(null);
  const fit = useFitScale(ref, inner, box, scalable);
  const clickable = "onClick" in m.select;
  return (
    <div
      ref={ref}
      data-element={box.id}
      data-kind="box"
      data-shape={box.shape ?? "card"}
      data-down={down ? "true" : undefined}
      className={`absolute box-border text-ink ${down ? "border-crit" : "border-line"} ${
        !m.linked || m.removed ? "border-dashed" : ""
      } ${instance && clickable ? "cursor-pointer focus-visible:ring-2 focus-visible:ring-in focus-visible:outline-none" : ""} ${className}`}
      style={{ left: box.x, top: box.y, width: box.w, height: fit.height, ...style }}
      {...m.select}
    >
      {scalable ? (
        <div
          ref={inner}
          data-scale={fit.scale > 1 ? fit.scale : undefined}
          style={
            fit.scale > 1
              ? {
                  width: `${100 / fit.scale}%`,
                  transform: `translateY(${fit.dy}px) scale(${fit.scale})`,
                  transformOrigin: "0 0",
                }
              : undefined
          }
        >
          {children}
        </div>
      ) : (
        children
      )}
    </div>
  );
}

/** The tab on the top edge: name, "· role" and status. */
export function NameTab({ m }: { m: BoxModel }) {
  const { box, name, down } = m;
  const status = box.show.status && !m.removed;
  return (
    <div
      data-tab=""
      className="absolute -top-[15px] left-3.5 z-[1] flex max-w-[calc(100%-28px)] items-baseline gap-1.5 rounded-t-lg border border-b-0 border-line bg-panel px-2.5 py-0.5 whitespace-nowrap"
    >
      <b className={`min-w-0 truncate text-[15px] ${down ? "text-crit-text" : ""}`} title={name}>
        {name}
      </b>
      {box.role && (
        <span className="min-w-0 truncate text-[12px] text-muted" title={box.role}>
          · {box.role}
        </span>
      )}
      {status && <StatusWord state={m.state} />}
    </div>
  );
}

/** The icon chip with the model, address (mono) and version lines. */
export function IdentityBlock({ m }: { m: BoxModel }) {
  const { box, identity, color, instance, removed, down } = m;
  const tinted = color && instance && !removed;
  const showLines = box.show.details && !removed;
  const empty = !identity.model && !identity.address && !identity.version;
  return (
    <div className="flex min-w-0 items-center gap-2.5">
      <span
        aria-hidden="true"
        className="flex h-[34px] w-[34px] shrink-0 items-center justify-center rounded-lg bg-chip text-muted"
        style={tinted ? { background: tint(color), color } : undefined}
      >
        <MapIcon icon={box.icon} size={24} />
      </span>
      <span className="min-w-0 flex-1 leading-[17px]">
        {removed && <Clip className="text-[12px] text-muted">Device removed</Clip>}
        {!removed && showLines && identity.model && (
          <Clip className={`font-bold ${down ? "text-crit-text" : ""}`}>{identity.model}</Clip>
        )}
        {!removed && showLines && identity.address && (
          <Clip className="font-mono text-[11px] text-out">{identity.address}</Clip>
        )}
        {!removed && showLines && identity.version && (
          <Clip className="text-[11px] text-muted">{identity.version}</Clip>
        )}
        {!removed && (empty || !showLines) && !instance && (
          <Clip className="text-[12px] text-muted">
            {m.linked ? "" : box.subtitle || "Not monitored"}
          </Clip>
        )}
      </span>
    </div>
  );
}

/** The legacy figures line: up to three live numbers of the linked device. */
export function FigureLine({ m }: { m: BoxModel }) {
  const { box, instance, live, down, color } = m;
  if (!instance || !box.show.figures || m.removed) return null;
  const figures = live.figures(box, instance);
  if (figures.length === 0) return null;
  return (
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
  );
}

const SECTION_TITLE = "mb-1 block text-[10px] font-bold tracking-wide text-muted uppercase";

/** The box's sections: tiles with live figures, services, and label/value lists. */
export function SectionsView({ m }: { m: BoxModel }) {
  const { box, live } = m;
  const sections = box.sections ?? [];
  if (m.removed) return null;
  return (
    <>
      {sections.map((section) => {
        const items = shownItems(section, box.instance_id, live);
        if (items.length === 0) return null;
        const grid: CSSProperties = {
          gridTemplateColumns: `repeat(${section.kind === "list" ? 1 : section.columns}, minmax(0, 1fr))`,
          // Tiles sit side by side at a size that fits their content, not stretched across the box.
          ...(section.kind === "tiles" ? { maxWidth: section.columns * 170 } : {}),
        };
        return (
          <div key={section.id} data-section={section.id} data-kind-section={section.kind}>
            {section.title && <Clip className={SECTION_TITLE}>{section.title}</Clip>}
            <div className="grid gap-1.5" style={grid}>
              {items.map(({ item, value, live: isLive }) => {
                const c = paint(item.color);
                if (section.kind === "list") {
                  return (
                    <div
                      key={item.id}
                      data-item={item.id}
                      className="flex min-w-0 items-center gap-2 text-[12px]"
                    >
                      {item.icon && (
                        <span className="shrink-0 text-muted" style={{ color: c }}>
                          <MapIcon icon={item.icon} size={14} />
                        </span>
                      )}
                      <Clip className="flex-1">{item.label}</Clip>
                      {value && (
                        <Clip
                          className="max-w-[45%] shrink-0 font-mono text-[11px]"
                          style={{ color: c }}
                        >
                          {value}
                        </Clip>
                      )}
                    </div>
                  );
                }
                if (section.kind === "services") {
                  return (
                    <div
                      key={item.id}
                      data-item={item.id}
                      className="flex min-w-0 items-center gap-2 rounded-md border bg-inset px-2 py-1.5"
                      style={{ borderColor: c ?? "var(--vt-line)" }}
                    >
                      {item.icon && (
                        <span
                          className="flex h-5 w-5 shrink-0 items-center justify-center text-muted"
                          style={{ color: c }}
                        >
                          <MapIcon icon={item.icon} size={18} />
                        </span>
                      )}
                      <span className="min-w-0 flex-1 leading-[15px]">
                        <Clip className="text-[12px] font-bold">{item.label}</Clip>
                        {(item.sub || value) && (
                          <Clip className="text-[11px] text-muted">
                            {[value, item.sub].filter(Boolean).join(" · ")}
                          </Clip>
                        )}
                      </span>
                    </div>
                  );
                }
                return (
                  <div
                    key={item.id}
                    data-item={item.id}
                    className="min-w-0 rounded-md border bg-inset px-2.5 py-1.5"
                    style={{ borderColor: c ?? "var(--vt-line)" }}
                  >
                    <span className="flex items-center gap-1.5">
                      <Clip
                        className="text-[10px] font-bold tracking-wide text-muted uppercase"
                        style={{ color: c }}
                      >
                        {item.label}
                      </Clip>
                      {isLive && (
                        <span
                          data-live=""
                          aria-label="live"
                          className="h-1.5 w-1.5 shrink-0 rounded-full bg-ok"
                        />
                      )}
                    </span>
                    <Clip className="text-[15px] leading-[19px] font-bold">{value}</Clip>
                    {item.sub && <Clip className="text-[11px] text-muted">{item.sub}</Clip>}
                  </div>
                );
              })}
            </div>
          </div>
        );
      })}
    </>
  );
}
