import type { ReactNode } from "react";
import { Link } from "react-router";
import type { MapBox, MapCable, MapDocument, MapTable, MapText } from "../../api";
import { colorVar, deviceColor, isDeviceColor } from "../../app/deviceColors";
import { drawingOrder, speedLabel } from "./geometry";
import { StatusWord } from "./MapElements";
import { instanceAddress } from "./mapLive";
import type { MapLive } from "./mapLive";

interface Row {
  box: MapBox;
  name: string;
  state: string | null;
  detail: string;
  to: string | null;
  color: string | undefined;
}

function rows(doc: MapDocument, live: MapLive): Row[] {
  const nameOf = new Map<string, string>();
  for (const e of doc.elements) {
    if (e.kind === "box" || e.kind === "frame") {
      nameOf.set(e.id, e.label || live.instance(e.instance_id)?.name || "");
    }
  }
  const cables = doc.elements.filter((e): e is MapCable => e.kind === "cable");
  const boxes = drawingOrder(doc.elements.filter((e): e is MapBox => e.kind === "box"));
  return boxes.map((box) => {
    const instance = live.instance(box.instance_id);
    const state = live.state(box.instance_id);
    const via = cables
      .filter((c) => c.from.element === box.id || c.to.element === box.id)
      .map((c) => {
        const other = c.from.element === box.id ? c.to.element : c.from.element;
        const name = nameOf.get(other);
        return [name ? `via ${name}` : "", c.speed ? speedLabel(c.speed) : ""]
          .filter(Boolean)
          .join(" · ");
      });
    let head: string;
    if (state === "removed") head = "Device removed";
    else if (instance)
      head = [live.typeName(instance.type_id), instanceAddress(instance)].join(" · ");
    else head = box.subtitle || "Not monitored";
    return {
      box,
      name: box.label || instance?.name || "",
      state,
      detail: [head, ...via].filter(Boolean).join(" · "),
      to: instance ? `/devices/${instance.id}` : null,
      color: (isDeviceColor(box.color) ? colorVar(box.color) : undefined) ?? deviceColor(instance),
    };
  });
}

function RowView({ row }: { row: Row }) {
  const down = row.state === "down";
  const body = (
    <span className="flex-1">
      <b className={down ? "text-crit-text" : row.state === "removed" ? "text-muted" : ""}>
        {row.name}
      </b>{" "}
      <StatusWord state={row.state} />
      <span className="block text-[12px] text-muted">{row.detail}</span>
    </span>
  );
  const cls = "flex items-center gap-2.5 border-l-[3px] px-3 py-2.5 text-ink";
  const style = { borderLeftColor: down ? "var(--vt-crit)" : (row.color ?? "transparent") };
  return row.to ? (
    <Link to={row.to} data-row={row.box.id} className={cls} style={style}>
      {body}
      <span className="text-muted">›</span>
    </Link>
  ) : (
    <div data-row={row.box.id} className={cls} style={style}>
      {body}
    </div>
  );
}

function List({ children }: { children: ReactNode }) {
  return (
    <div className="flex flex-col divide-y divide-line overflow-hidden rounded-[10px] border border-line bg-panel">
      {children}
    </div>
  );
}

const Heading = ({ children }: { children: string }) => (
  <h2 className="text-xs font-bold tracking-wide text-muted uppercase">{children}</h2>
);

function Note({ el }: { el: MapText | MapTable }) {
  const summary =
    el.kind === "table" ? el.title || "Table" : el.text.split("\n")[0]?.slice(0, 40) || "Note";
  return (
    <details className="px-3 py-2.5">
      <summary className="cursor-pointer">{summary}</summary>
      {el.kind === "text" ? (
        <p className="mt-2 text-sm whitespace-pre-wrap">{el.text}</p>
      ) : (
        <table className="mt-2 w-full border-collapse text-[13px]">
          {el.columns.length > 0 && (
            <thead>
              <tr className="text-left text-[12px] text-muted">
                {el.columns.map((c, i) => (
                  <th key={i} className="py-1 pr-2">
                    {c}
                  </th>
                ))}
              </tr>
            </thead>
          )}
          <tbody>
            {el.rows.map((r, i) => (
              <tr key={i} className="border-t border-line">
                {r.cells.map((cell, j) => (
                  <td key={j} className="py-1 pr-2">
                    {cell}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </details>
  );
}

/** Phone width: no drawing, a list. Problems first, then the devices in drawing order. */
export function MapPhoneList({ doc, live }: { doc: MapDocument; live: MapLive }) {
  const all = rows(doc, live);
  const problems = all.filter((r) => r.state === "down");
  const others = all.filter((r) => r.state !== "down");
  const notes = drawingOrder(
    doc.elements.filter((e): e is MapText | MapTable => e.kind === "text" || e.kind === "table"),
  );
  return (
    <section aria-label="Map devices" className="flex flex-col gap-3.5">
      {problems.length > 0 && (
        <>
          <Heading>Problems</Heading>
          <List>
            {problems.map((r) => (
              <RowView key={r.box.id} row={r} />
            ))}
          </List>
        </>
      )}
      <Heading>Devices</Heading>
      {others.length === 0 ? (
        <p className="text-sm text-muted">No other devices on this map.</p>
      ) : (
        <List>
          {others.map((r) => (
            <RowView key={r.box.id} row={r} />
          ))}
        </List>
      )}
      {notes.length > 0 && (
        <>
          <Heading>Notes and tables</Heading>
          <List>
            {notes.map((n) => (
              <Note key={n.id} el={n} />
            ))}
          </List>
        </>
      )}
      <p className="text-center text-[13px] text-muted">
        Open Vantage on a larger screen to see the drawing.
      </p>
    </section>
  );
}
