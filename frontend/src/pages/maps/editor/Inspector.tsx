// The right-hand panel of the editor: the settings of the selected element. Every change is
// handed to `patch` (one undo step per field while typing). Text is plain text only.

import { useState } from "react";
import type {
  Instance,
  MapBox,
  MapDocument,
  MapElement,
  MapFrame,
  MapTable,
  MapText,
} from "../../../api";
import { buttonSecondary, input } from "../../../components/styles";
import { FIGURES } from "../figures";
import type { MapLive } from "../mapLive";
import { BoxContentSettings, Row, SectionsEditor } from "./BoxContentSettings";
import { CableSettings } from "./CableSettings";
import { Check, ColorPicker, Collapse, Field, labelText, section } from "./fields";
import { FrameGroups } from "./FrameGroups";
import { ManualTiles, ProxmoxGuestList } from "./GuestsSettings";
import { IconPicker } from "./IconPicker";
import { contentSized } from "../boxFacts";
import { clamp, MAX_SIZE, MIN_SIZE } from "./model";
import { LookAndPorts } from "./LookAndPorts";
import { emptyPanel } from "../panel";

/** Integrations whose ports the map draws like the dashboard's switch card. */
const SWITCH_TYPES = new Set(["snmp_device", "mikrotik_swos"]);

/**
 * Linking a box without ports to a switch gives it the switch's own front panel, read live
 * from the device (as on its dashboard card). A box that already has ports keeps them.
 */
function switchPanel(box: MapBox, id: string | null, instances: readonly Instance[]) {
  const type = instances.find((i) => i.id === id)?.type_id;
  if (box.panel || !type || !SWITCH_TYPES.has(type)) return {};
  return {
    shape: box.shape === "card" ? ("device" as const) : box.shape,
    panel: { ...emptyPanel("device"), look: "rack" as const, captions: "cables" as const },
  };
}

const MAX_COLUMNS = 8;
const MAX_ROWS = 100;

/** A number box that commits on blur or Enter, so typing "2" on the way to "200" is fine. */
function NumberField({
  label,
  value,
  onCommit,
}: {
  label: string;
  value: number;
  onCommit: (v: number) => void;
}) {
  const [draft, setDraft] = useState<string | null>(null);
  const commit = () => {
    const n = Number(draft);
    if (draft !== null && Number.isFinite(n)) onCommit(clamp(Math.round(n), MIN_SIZE, MAX_SIZE));
    setDraft(null);
  };
  return (
    <Field label={label}>
      <input
        type="number"
        className={input}
        value={draft ?? String(value)}
        min={MIN_SIZE}
        max={MAX_SIZE}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => e.key === "Enter" && commit()}
      />
    </Field>
  );
}

function DevicePicker({
  value,
  instances,
  onlyType,
  noneLabel,
  onChange,
}: {
  value: string | null;
  instances: readonly Instance[];
  onlyType?: string;
  noneLabel: string;
  onChange: (id: string | null) => void;
}) {
  const options = instances.filter((i) => !onlyType || i.type_id === onlyType);
  const known = value === null || instances.some((i) => i.id === value);
  return (
    <select
      className={input}
      value={value ?? ""}
      onChange={(e) => onChange(e.target.value || null)}
    >
      <option value="">{noneLabel}</option>
      {!known && <option value={value}>Removed device</option>}
      {options.map((i) => (
        <option key={i.id} value={i.id}>
          {i.name}
        </option>
      ))}
    </select>
  );
}

type Patch = (patch: Record<string, unknown>, key?: string) => void;

function BoxSettings({
  box,
  doc,
  instances,
  live,
  patch,
  edit,
}: {
  box: MapBox;
  doc: MapDocument;
  instances: readonly Instance[];
  live: MapLive;
  patch: Patch;
  edit: (next: MapDocument) => void;
}) {
  const [tab, setTab] = useState<"box" | "look">("box");
  const instance = instances.find((i) => i.id === box.instance_id);
  const defs = instance ? (FIGURES[instance.type_id] ?? []) : [];
  const toggleFigure = (id: string, on: boolean) => {
    const next = on ? [...box.figures, id] : box.figures.filter((f) => f !== id);
    patch({ figures: next });
  };
  const tabs = (
    <div role="tablist" aria-label="Box settings" className="flex gap-4 border-b border-line">
      {(
        [
          ["box", "Box"],
          ["look", "Look and ports"],
        ] as const
      ).map(([id, text]) => (
        <button
          key={id}
          type="button"
          role="tab"
          aria-selected={tab === id}
          onClick={() => setTab(id)}
          className={`-mb-px border-b-2 py-1.5 text-sm focus-visible:ring-2 focus-visible:ring-in focus-visible:outline-none ${
            tab === id ? "border-in font-bold text-ink" : "border-transparent text-muted"
          }`}
        >
          {text}
        </button>
      ))}
    </div>
  );
  if (tab === "look") {
    return (
      <>
        {tabs}
        <LookAndPorts
          doc={doc}
          box={box}
          instance={instance}
          live={live}
          patch={patch}
          edit={edit}
        />
      </>
    );
  }
  return (
    <>
      {tabs}
      <Collapse id="box-identity" title="Identity">
        <Field label="Linked device">
          <DevicePicker
            value={box.instance_id}
            instances={instances}
            noneLabel="None (not monitored)"
            onChange={(id) =>
              patch({ instance_id: id, figures: [], ...switchPanel(box, id, instances) })
            }
          />
        </Field>
        <p className="text-xs text-muted">
          Live status and figures come from this device. Pick None for things Vantage doesn&apos;t
          watch, like a modem or a KVM.
        </p>
        <Field label="Label">
          <input
            className={input}
            value={box.label}
            maxLength={64}
            onChange={(e) => patch({ label: e.target.value }, `${box.id}:label`)}
          />
        </Field>
        {box.instance_id && box.label !== "" && (
          <button type="button" className={buttonSecondary} onClick={() => patch({ label: "" })}>
            Use the device&apos;s name
          </button>
        )}
        {!box.instance_id && (
          <Field label="Subtitle">
            <input
              className={input}
              value={box.subtitle}
              maxLength={64}
              onChange={(e) => patch({ subtitle: e.target.value }, `${box.id}:subtitle`)}
            />
          </Field>
        )}
        <div>
          <span className={labelText}>Icon</span>
          <IconPicker value={box.icon} onChange={(icon) => patch({ icon })} />
        </div>
        <div>
          <span className={labelText}>Colour</span>
          <ColorPicker
            value={box.color}
            noneLabel={box.instance_id ? "Device colour" : "No colour"}
            onChange={(c) => patch({ color: c })}
          />
        </div>
      </Collapse>
      <Collapse id="box-content" title="Content">
        <BoxContentSettings box={box} instance={instance} patch={patch} />
      </Collapse>
      <Collapse id="box-more" title="More: what shows on the box" open={false}>
        {box.instance_id && (
          <fieldset className="flex flex-col gap-1.5">
            <legend className={labelText}>Show on the box</legend>
            <Check
              label="Type and address"
              checked={box.show.details}
              onChange={(v) => patch({ show: { ...box.show, details: v } })}
            />
            <Check
              label="Status (UP / DOWN)"
              checked={box.show.status}
              onChange={(v) => patch({ show: { ...box.show, status: v } })}
            />
            <Check
              label="Figures"
              checked={box.show.figures}
              onChange={(v) => patch({ show: { ...box.show, figures: v } })}
            />
          </fieldset>
        )}
        {box.instance_id && defs.length > 0 && (
          <fieldset className="flex flex-col gap-1.5">
            <legend className={labelText}>Figures, up to 3 (none ticked = the usual ones)</legend>
            {defs.map((d) => {
              const on = box.figures.includes(d.id);
              return (
                <Check
                  key={d.id}
                  label={d.label}
                  checked={on}
                  disabled={!on && box.figures.length >= 3}
                  onChange={(v) => toggleFigure(d.id, v)}
                />
              );
            })}
          </fieldset>
        )}
      </Collapse>
    </>
  );
}

function FrameSettings({
  frame,
  instances,
  patch,
}: {
  frame: MapFrame;
  instances: readonly Instance[];
  patch: Patch;
}) {
  const isProxmox = instances.find((i) => i.id === frame.instance_id)?.type_id === "proxmox";
  return (
    <>
      <Field label="Label">
        <input
          className={input}
          value={frame.label}
          maxLength={64}
          onChange={(e) => patch({ label: e.target.value }, `${frame.id}:label`)}
        />
      </Field>
      <Field label="Proxmox instance">
        <DevicePicker
          value={frame.instance_id}
          instances={instances}
          onlyType="proxmox"
          noneLabel="None"
          onChange={(id) => patch({ instance_id: id })}
        />
      </Field>
      <Check
        label="Fill in guests automatically"
        checked={frame.guests.auto}
        onChange={(v) => patch({ guests: { ...frame.guests, auto: v } })}
      />
      {!frame.guests.auto && (
        <p className="text-xs text-muted">Auto-fill is off: only your own tiles show.</p>
      )}
      {frame.guests.auto && frame.instance_id && isProxmox && (
        <ProxmoxGuestList
          guests={frame.guests}
          instanceId={frame.instance_id}
          set={(guests, key) => patch({ guests }, key)}
        />
      )}
      <ManualTiles frame={frame} set={(guests, key) => patch({ guests }, key)} />
      <FrameGroups
        frame={frame}
        proxmoxId={frame.guests.auto && frame.instance_id && isProxmox ? frame.instance_id : null}
        set={(groups, key) => patch({ groups }, key)}
      />
      <Check
        label="CPU and memory bars on the guests"
        checked={frame.bars}
        onChange={(v) => patch({ bars: v })}
      />
      <Check
        label="Side column (identity and sections)"
        checked={frame.side !== null}
        onChange={(on) =>
          patch({ side: on ? { model: "", address: "", version: "", sections: [] } : null })
        }
      />
      {frame.side && (
        <>
          {(["model", "address", "version"] as const).map((k) => (
            <Row
              key={k}
              label={k[0]?.toUpperCase() + k.slice(1)}
              value={frame.side?.[k] ?? ""}
              max={64}
              onChange={(v) =>
                patch(
                  { side: { ...(frame.side as NonNullable<MapFrame["side"]>), [k]: v } },
                  `${frame.id}:side:${k}`,
                )
              }
            />
          ))}
          <SectionsEditor
            sections={frame.side.sections}
            instance={instances.find((i) => i.id === frame.instance_id)}
            set={(sections) =>
              patch({ side: { ...(frame.side as NonNullable<MapFrame["side"]>), sections } })
            }
          />
        </>
      )}
      <div>
        <span className={labelText}>Colour</span>
        <ColorPicker
          value={frame.color}
          noneLabel={frame.instance_id ? "Device colour" : "No colour"}
          onChange={(c) => patch({ color: c })}
        />
      </div>
    </>
  );
}

function TextSettings({ el, patch }: { el: MapText; patch: Patch }) {
  return (
    <>
      <Field label="Text">
        <textarea
          className={`${input} h-28`}
          value={el.text}
          maxLength={4000}
          onChange={(e) => patch({ text: e.target.value }, `${el.id}:text`)}
        />
      </Field>
      <Field label="Size">
        <select className={input} value={el.size} onChange={(e) => patch({ size: e.target.value })}>
          <option value="s">Small</option>
          <option value="m">Medium</option>
          <option value="l">Large</option>
        </select>
      </Field>
      <Check
        label="Panel behind the text"
        checked={el.panel}
        onChange={(v) => patch({ panel: v })}
      />
    </>
  );
}

function TableSettings({ el, patch }: { el: MapTable; patch: Patch }) {
  const setColumns = (columns: string[], rows = el.rows, badges = el.badges) =>
    patch({ columns, rows, badges });
  const cell = (cells: string[], c: number) => cells[c] ?? "";
  const moveRow = (r: number, by: number) => {
    const rows = [...el.rows];
    const [row] = rows.splice(r, 1);
    if (row) rows.splice(r + by, 0, row);
    patch({ rows });
  };
  return (
    <>
      <Field label="Title">
        <input
          className={input}
          value={el.title}
          maxLength={64}
          onChange={(e) => patch({ title: e.target.value }, `${el.id}:title`)}
        />
      </Field>
      <fieldset className="flex flex-col gap-1.5">
        <legend className={labelText}>Columns</legend>
        {el.columns.map((name, c) => (
          <div key={c} className="flex flex-wrap items-center gap-1.5">
            <input
              className={input}
              aria-label={`Column ${c + 1}`}
              value={name}
              maxLength={32}
              onChange={(e) => setColumns(el.columns.map((n, i) => (i === c ? e.target.value : n)))}
            />
            <button
              type="button"
              aria-label={`Remove column ${c + 1}`}
              className={buttonSecondary}
              onClick={() =>
                setColumns(
                  el.columns.filter((_, i) => i !== c),
                  el.rows.map((r) => ({ ...r, cells: r.cells.filter((_, i) => i !== c) })),
                  el.badges.filter((b) => b !== c).map((b) => (b > c ? b - 1 : b)),
                )
              }
            >
              ×
            </button>
            <Check
              label={`Column ${c + 1} as badges`}
              checked={el.badges.includes(c)}
              onChange={(on) =>
                patch({ badges: on ? [...el.badges, c] : el.badges.filter((b) => b !== c) })
              }
            />
          </div>
        ))}
        <button
          type="button"
          className={buttonSecondary}
          disabled={el.columns.length >= MAX_COLUMNS}
          onClick={() => setColumns([...el.columns, ""])}
        >
          Add column
        </button>
      </fieldset>
      <fieldset className="flex flex-col gap-2">
        <legend className={labelText}>Rows</legend>
        {el.rows.map((row, r) => (
          <div key={r} className="flex flex-col gap-1.5 rounded-md border border-line p-2">
            {el.columns.map((_, c) => (
              <input
                key={c}
                className={input}
                aria-label={`Row ${r + 1}, column ${c + 1}`}
                value={cell(row.cells, c)}
                maxLength={128}
                onChange={(e) =>
                  patch({
                    rows: el.rows.map((x, i) =>
                      i === r
                        ? {
                            ...x,
                            cells: el.columns.map((_, k) =>
                              k === c ? e.target.value : cell(x.cells, k),
                            ),
                          }
                        : x,
                    ),
                  })
                }
              />
            ))}
            <ColorPicker
              value={row.color}
              noneLabel="No colour"
              onChange={(color) =>
                patch({ rows: el.rows.map((x, i) => (i === r ? { ...x, color } : x)) })
              }
            />
            <div className="flex gap-1.5">
              <button
                type="button"
                aria-label={`Move row ${r + 1} up`}
                className={buttonSecondary}
                disabled={r === 0}
                onClick={() => moveRow(r, -1)}
              >
                ↑
              </button>
              <button
                type="button"
                aria-label={`Move row ${r + 1} down`}
                className={buttonSecondary}
                disabled={r === el.rows.length - 1}
                onClick={() => moveRow(r, 1)}
              >
                ↓
              </button>
              <button
                type="button"
                aria-label={`Remove row ${r + 1}`}
                className={buttonSecondary}
                onClick={() => patch({ rows: el.rows.filter((_, i) => i !== r) })}
              >
                ×
              </button>
            </div>
          </div>
        ))}
        <button
          type="button"
          className={buttonSecondary}
          disabled={el.rows.length >= MAX_ROWS}
          onClick={() =>
            patch({ rows: [...el.rows, { cells: el.columns.map(() => ""), color: null }] })
          }
        >
          Add row
        </button>
      </fieldset>
    </>
  );
}

const KIND_NAME: Record<MapElement["kind"], string> = {
  box: "Device box",
  frame: "Frame",
  text: "Text",
  table: "Table",
  legend: "Legend",
  networks: "Networks",
  cable: "Cable",
};

export function Inspector({
  doc,
  selected,
  instances,
  live,
  patch,
  edit,
  remove,
}: {
  doc: MapDocument;
  selected: string[];
  instances: readonly Instance[];
  live: MapLive;
  patch: (id: string, patch: Record<string, unknown>, key?: string) => void;
  /** A change to the whole document (ports and VLANs touch cables too). */
  edit: (next: MapDocument) => void;
  remove: () => void;
}) {
  const els = selected.flatMap((id) => doc.elements.filter((e) => e.id === id));
  const el = els.length === 1 ? els[0] : undefined;
  if (els.length === 0) {
    return (
      <p className="text-sm text-muted">
        Nothing selected. Click an element to change it, or pick a tool to add one.
      </p>
    );
  }
  if (!el) {
    return (
      <div className="flex flex-col gap-3">
        <h2 className="text-sm font-semibold">{els.length} elements selected</h2>
        <p className="text-xs text-muted">
          Drag to move them together. Shift-click adds or removes one.
        </p>
        <button type="button" className={buttonSecondary} onClick={remove}>
          Remove from map
        </button>
      </div>
    );
  }
  const set: Patch = (p, key) => patch(el.id, p, key);
  return (
    <div className="flex flex-col gap-3">
      <h2 className="text-sm font-semibold">{KIND_NAME[el.kind]}</h2>
      {el.kind === "box" && (
        <BoxSettings box={el} doc={doc} instances={instances} live={live} patch={set} edit={edit} />
      )}
      {el.kind === "frame" && <FrameSettings frame={el} instances={instances} patch={set} />}
      {el.kind === "text" && <TextSettings el={el} patch={set} />}
      {el.kind === "table" && <TableSettings el={el} patch={set} />}
      {el.kind === "legend" && (
        <p className="text-xs text-muted">
          The legend lists the cable types this map uses. Nothing to set.
        </p>
      )}
      {el.kind === "networks" && (
        <p className="text-xs text-muted">
          Lists the VLANs of this map (name, note and subnet are set under VLANs). Nothing to set.
        </p>
      )}
      {el.kind === "cable" && (
        <CableSettings cable={el} doc={doc} instances={instances} live={live} patch={set} />
      )}
      {el.kind !== "cable" && (
        <div className={`${section} grid grid-cols-2 gap-3`}>
          <NumberField label="W" value={el.w} onCommit={(w) => set({ w })} />
          {contentSized(el) ? (
            <p className="self-end text-xs text-muted">Height follows the content.</p>
          ) : (
            <NumberField label="H" value={el.h} onCommit={(h) => set({ h })} />
          )}
          {el.kind === "box" && !contentSized(el) && (
            <p className="col-span-2 text-xs text-muted">
              Taller than its content, the box scales the content up to fill it.
            </p>
          )}
        </div>
      )}
      <div className={section}>
        <button type="button" className={buttonSecondary} onClick={remove}>
          {el.kind === "cable" ? "Remove cable" : "Remove from map"}
        </button>
        {el.kind !== "cable" && (
          <p className="text-xs text-muted">
            Removing it only changes the map
            {el.kind === "box" || el.kind === "frame"
              ? ", and the cables attached to it. The device keeps being monitored"
              : ""}
            .
          </p>
        )}
      </div>
    </div>
  );
}
