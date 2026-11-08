// The "Look and ports" tab of a box: its shape, the look of the front panel, where its ports
// come from (a model from the preset list, the device's own interfaces, or blank), the port
// groups, size / place / captions / numbers, the port table and, for a NAS, the drive bays.
// Presets are copied into the map; after that it is all ordinary, editable data.

import { useState } from "react";
import type { Instance, MapBox, MapDocument, MapNas, MapPanel, MapPort } from "../../../api";
import { buttonSecondary, input } from "../../../components/styles";
import {
  emptyPanel,
  fromDevice,
  isCage,
  KIND_LABEL,
  MAX_GROUP_COUNT,
  MAX_GROUPS,
  MAX_PANEL_PORTS,
  newGroup,
  portNumber,
  regeneratePorts,
} from "../panel";
import type { MapLive } from "../mapLive";
import { hasPorts } from "../devicePorts";
import { Check, CommitText, labelText } from "./fields";
import { setBoxPorts, vlanSummary } from "./mapOps";
import { patchElement } from "./model";
import { PortVlans } from "./PortVlans";
import { applyPreset, PRESETS } from "./presets";
import { LookThumb, ShapeThumb } from "./thumbs";

type Edit = (next: MapDocument) => void;
type Patch = (patch: Record<string, unknown>, key?: string) => void;

const SHAPES: readonly { id: NonNullable<MapBox["shape"]>; label: string }[] = [
  { id: "card", label: "Card" },
  { id: "cloud", label: "Cloud" },
  { id: "device", label: "Device" },
  { id: "nas", label: "NAS bays" },
  { id: "ap", label: "Access point" },
];

const LOOKS: readonly { id: MapPanel["look"]; label: string }[] = [
  { id: "rack", label: "Rack 1U" },
  { id: "desktop", label: "Desktop" },
  { id: "flat", label: "Flat" },
];

const GROUP_KINDS = ["rj45", "sfp", "sfp_plus", "sfp28", "qsfp", "console", "usb"] as const;

function Picker<T extends string>({
  label,
  value,
  options,
  onChange,
  thumb,
  columns = 3,
}: {
  label: string;
  value: T;
  options: readonly { id: T; label: string }[];
  onChange: (v: T) => void;
  thumb: (id: T) => React.ReactNode;
  columns?: number;
}) {
  return (
    <div
      role="group"
      aria-label={label}
      className="grid gap-1.5"
      style={{ gridTemplateColumns: `repeat(${columns}, minmax(0, 1fr))` }}
    >
      {options.map((o) => (
        <button
          key={o.id}
          type="button"
          aria-pressed={value === o.id}
          aria-label={o.label}
          onClick={() => onChange(o.id)}
          className={`flex flex-col items-center gap-0.5 rounded-md border p-1.5 text-[11px] focus-visible:ring-2 focus-visible:ring-in focus-visible:outline-none ${
            value === o.id ? "border-in bg-chip text-ink" : "border-line text-muted hover:bg-chip"
          }`}
        >
          {thumb(o.id)}
          {o.label}
        </button>
      ))}
    </div>
  );
}

function Segmented<T extends string>({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: T;
  options: readonly { id: T; label: string }[];
  onChange: (v: T) => void;
}) {
  return (
    <div>
      <span className={labelText}>{label}</span>
      <div
        role="group"
        aria-label={label}
        className="flex overflow-hidden rounded-md border border-line"
      >
        {options.map((o) => (
          <button
            key={o.id}
            type="button"
            aria-pressed={value === o.id}
            onClick={() => onChange(o.id)}
            className={`flex-1 px-2 py-1.5 text-xs focus-visible:ring-2 focus-visible:ring-in focus-visible:outline-none ${
              value === o.id ? "bg-chip font-bold text-ink" : "text-muted hover:bg-chip"
            }`}
          >
            {o.label}
          </button>
        ))}
      </div>
    </div>
  );
}

/** A number box that commits on blur or Enter (so typing "2" on the way to "24" is fine). */
function NumField({
  label,
  value,
  min,
  max,
  onCommit,
}: {
  label: string;
  value: number;
  min: number;
  max: number;
  onCommit: (v: number) => void;
}) {
  const [draft, setDraft] = useState<string | null>(null);
  const commit = () => {
    const n = Number(draft);
    if (draft !== null && draft !== "" && Number.isFinite(n)) {
      onCommit(Math.min(max, Math.max(min, Math.round(n))));
    }
    setDraft(null);
  };
  return (
    <input
      type="number"
      className={input}
      aria-label={label}
      value={draft ?? String(value)}
      min={min}
      max={max}
      onChange={(e) => setDraft(e.target.value)}
      onBlur={commit}
      onKeyDown={(e) => e.key === "Enter" && commit()}
    />
  );
}

export function LookAndPorts({
  doc,
  box,
  instance,
  live,
  patch,
  edit,
}: {
  doc: MapDocument;
  box: MapBox;
  instance: Instance | undefined;
  live: MapLive;
  patch: Patch;
  edit: Edit;
}) {
  const panel = box.panel;
  const shape = box.shape ?? "card";
  const device = live.ports(box.instance_id) ?? [];
  const setPanel = (next: MapPanel | null) => edit(setBoxPorts(doc, box.id, next));
  const setGroups = (groups: MapPanel["groups"]) =>
    setPanel(regeneratePorts({ ...(panel ?? emptyPanel()), source: "manual", groups }));
  const setPorts = (ports: MapPort[]) => panel && setPanel({ ...panel, ports });
  const setShape = (next: NonNullable<MapBox["shape"]>) =>
    patch({
      shape: next,
      ...(next === "nas" && !box.nas ? { nas: { bays: 4, bay_disks: [] } } : {}),
    });

  const startFrom = (choice: string) => {
    if (choice === "blank") {
      setPanel({ ...(panel ?? emptyPanel()), source: "manual", groups: [], ports: [] });
    } else if (choice === "device") {
      const cabled = new Set(
        doc.elements.flatMap((e) =>
          e.kind === "cable"
            ? [e.from, e.to].flatMap((end) =>
                end.element === box.id && end.port ? [end.port] : [],
              )
            : [],
        ),
      );
      const read = fromDevice(device, panel?.ports ?? [], cabled);
      setPanel({ ...(panel ?? emptyPanel("device")), source: "device", ...read });
    } else {
      const preset = PRESETS.find((p) => p.id === choice);
      if (!preset) return;
      const { panel: next, ...rest } = applyPreset(box, preset);
      edit(setBoxPorts(patchElement(doc, box.id, rest), box.id, next ?? null));
    }
  };

  const groups = panel?.groups ?? [];
  const total = groups.reduce((n, g) => n + g.count, 0);
  const changeGroup = (i: number, change: Partial<MapPanel["groups"][number]>) =>
    setGroups(groups.map((g, k) => (k === i ? { ...g, ...change } : g)));
  const moveGroup = (i: number, by: number) => {
    const list = [...groups];
    const [g] = list.splice(i, 1);
    if (g) list.splice(i + by, 0, g);
    setGroups(list);
  };

  return (
    <div className="flex flex-col gap-3">
      <div>
        <span className={labelText}>Shape</span>
        <Picker
          label="Shape"
          value={shape}
          options={SHAPES}
          onChange={setShape}
          thumb={(id) => <ShapeThumb shape={id} />}
          columns={3}
        />
      </div>
      {shape === "device" && (
        <div>
          <span className={labelText}>Panel look</span>
          <Picker
            label="Panel look"
            value={panel?.look ?? "flat"}
            options={LOOKS}
            onChange={(look) =>
              edit(patchElement(doc, box.id, { panel: { ...(panel ?? emptyPanel()), look } }))
            }
            thumb={(id) => <LookThumb look={id} />}
          />
        </div>
      )}
      {shape === "nas" && <NasSettings box={box} live={live} patch={patch} />}

      <fieldset className="flex flex-col gap-2 border-t border-line pt-3">
        <legend className={labelText}>Front panel</legend>
        <select
          className={input}
          aria-label="Start from"
          value=""
          onChange={(e) => e.target.value && startFrom(e.target.value)}
        >
          <option value="">Start from…</option>
          <optgroup label="Model list">
            {PRESETS.map((p) => (
              <option key={p.id} value={p.id}>
                {p.label}
              </option>
            ))}
          </optgroup>
          <option value="device" disabled={!instance || !hasPorts(instance) || device.length === 0}>
            Read from the device
          </option>
          <option value="blank">Blank</option>
        </select>
        <p className="text-xs text-muted">
          A model is only a starting point: it is copied into the map and stays editable. Reading
          from the device lists its interfaces (MikroTik, SNMP, pfSense, Linux).
        </p>
        {panel && (
          <>
            <span className={labelText}>
              Port groups · {total} {total === 1 ? "port" : "ports"}
            </span>
            {groups.map((g, i) => {
              const name = `Group ${i + 1}`;
              return (
                <div key={g.id} className="flex flex-col gap-1.5 rounded-md border border-line p-2">
                  <div className="grid grid-cols-[1fr_4.5rem_4.5rem] gap-1.5">
                    <select
                      className={input}
                      aria-label={`${name} kind`}
                      value={g.kind}
                      onChange={(e) => changeGroup(i, { kind: e.target.value as typeof g.kind })}
                    >
                      {GROUP_KINDS.map((k) => (
                        <option key={k} value={k}>
                          {KIND_LABEL[k]}
                        </option>
                      ))}
                    </select>
                    <NumField
                      label={`${name} count`}
                      value={g.count}
                      min={1}
                      max={Math.max(
                        1,
                        Math.min(MAX_GROUP_COUNT, MAX_PANEL_PORTS - (total - g.count)),
                      )}
                      onCommit={(count) => changeGroup(i, { count })}
                    />
                    <select
                      className={input}
                      aria-label={`${name} rows`}
                      value={g.rows}
                      onChange={(e) => changeGroup(i, { rows: Number(e.target.value) as 1 | 2 })}
                    >
                      <option value={1}>1 row</option>
                      <option value={2}>2 rows</option>
                    </select>
                  </div>
                  <div className="grid grid-cols-[4.5rem_1fr] gap-1.5">
                    <NumField
                      label={`${name} first number`}
                      value={g.first}
                      min={0}
                      max={999}
                      onCommit={(first) => changeGroup(i, { first })}
                    />
                    <CommitText
                      label={`${name} interface prefix`}
                      value={g.prefix}
                      maxLength={16}
                      accept={() => true}
                      onCommit={(prefix) => changeGroup(i, { prefix })}
                    />
                  </div>
                  {g.rows === 2 && (
                    <Check
                      label={`${name}: odd numbers on top`}
                      checked={g.odd_top}
                      onChange={(odd_top) => changeGroup(i, { odd_top })}
                    />
                  )}
                  <div className="flex gap-1.5">
                    <button
                      type="button"
                      aria-label={`Move ${name} up`}
                      className={buttonSecondary}
                      disabled={i === 0}
                      onClick={() => moveGroup(i, -1)}
                    >
                      ↑
                    </button>
                    <button
                      type="button"
                      aria-label={`Move ${name} down`}
                      className={buttonSecondary}
                      disabled={i === groups.length - 1}
                      onClick={() => moveGroup(i, 1)}
                    >
                      ↓
                    </button>
                    <button
                      type="button"
                      aria-label={`Remove ${name}`}
                      className={buttonSecondary}
                      onClick={() => setGroups(groups.filter((_, k) => k !== i))}
                    >
                      ×
                    </button>
                  </div>
                </div>
              );
            })}
          </>
        )}
        <div className="flex flex-wrap items-center gap-1.5 text-xs text-muted">
          Add group:
          {GROUP_KINDS.map((kind) => (
            <button
              key={kind}
              type="button"
              aria-label={`Add ${KIND_LABEL[kind]} group`}
              className="rounded border border-line px-1.5 py-0.5 text-in hover:bg-chip disabled:opacity-50"
              disabled={groups.length >= MAX_GROUPS || total >= MAX_PANEL_PORTS}
              onClick={() => setGroups([...groups, newGroup(groups, kind)])}
            >
              {KIND_LABEL[kind]}
            </button>
          ))}
        </div>
        {panel && (
          <>
            <div className="grid grid-cols-2 gap-2">
              <Segmented
                label="Jack size"
                value={panel.size}
                options={[
                  { id: "s", label: "S" },
                  { id: "m", label: "M" },
                  { id: "l", label: "L" },
                ]}
                onChange={(size) => setPanel({ ...panel, size })}
              />
              <Segmented
                label="Place"
                value={panel.place}
                options={[
                  { id: "bottom", label: "Bottom" },
                  { id: "top", label: "Top" },
                  { id: "side", label: "Side" },
                ]}
                onChange={(place) => setPanel({ ...panel, place })}
              />
              <Segmented
                label="Captions"
                value={panel.captions}
                options={[
                  { id: "cables", label: "From cables" },
                  { id: "typed", label: "Typed" },
                  { id: "off", label: "Off" },
                ]}
                onChange={(captions) => setPanel({ ...panel, captions })}
              />
              <Segmented
                label="Numbers"
                value={panel.numbers ? "on" : "off"}
                options={[
                  { id: "on", label: "On" },
                  { id: "off", label: "Off" },
                ]}
                onChange={(v) => setPanel({ ...panel, numbers: v === "on" })}
              />
            </div>
            <PortTable doc={doc} panel={panel} setPorts={setPorts} />
            <button type="button" className={buttonSecondary} onClick={() => setPanel(null)}>
              Remove the front panel
            </button>
          </>
        )}
      </fieldset>
    </div>
  );
}

function PortTable({
  doc,
  panel,
  setPorts,
}: {
  doc: MapDocument;
  panel: MapPanel;
  setPorts: (ports: MapPort[]) => void;
}) {
  const change = (id: string, patch: Partial<MapPort>) =>
    setPorts(panel.ports.map((p) => (p.id === id ? { ...p, ...patch } : p)));
  if (panel.ports.length === 0) return null;
  return (
    <div>
      <span className={labelText}>Ports · {panel.ports.length}</span>
      <div className="max-h-80 overflow-y-auto rounded-md border border-line">
        {panel.ports.map((p) => {
          const group = panel.groups.find((g) => g.id === p.group);
          const name = group ? `${KIND_LABEL[group.kind]} ${portNumber(group, p)}` : p.id;
          return (
            <div
              key={p.id}
              data-port-row={p.id}
              className="flex flex-col gap-1 border-b border-line p-2 last:border-b-0"
            >
              <div className="grid grid-cols-[3.5rem_1fr_1fr] items-center gap-1.5">
                <b className="truncate text-xs" title={name}>
                  {group && isCage(group.kind) ? name : group ? portNumber(group, p) : p.id}
                </b>
                <CommitText
                  label={`${name} interface`}
                  value={p.interface}
                  maxLength={64}
                  accept={() => true}
                  onCommit={(v) => change(p.id, { interface: v })}
                />
                <CommitText
                  label={`${name} caption`}
                  value={p.caption}
                  maxLength={16}
                  accept={() => true}
                  onCommit={(v) => change(p.id, { caption: v })}
                />
              </div>
              <div className="flex items-start justify-between gap-2">
                <details className="min-w-0 flex-1">
                  <summary className="cursor-pointer text-xs text-muted">
                    VLANs{vlanSummary(p.vlans) ? `: ${vlanSummary(p.vlans)}` : ""}
                  </summary>
                  <PortVlans
                    name={name}
                    vlans={p.vlans}
                    choices={doc.vlans}
                    onChange={(vlans) => change(p.id, { vlans })}
                  />
                </details>
                <Check
                  label={`${name} hidden`}
                  checked={p.hidden}
                  onChange={(hidden) => change(p.id, { hidden })}
                />
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

/** Bay count and the matching of bays to the SMART disks of the device. */
function NasSettings({ box, live, patch }: { box: MapBox; live: MapLive; patch: Patch }) {
  const nas: MapNas = box.nas ?? { bays: 4, bay_disks: [] };
  const disks = live.smartDisks(box.instance_id);
  const listId = `smart-disks-${box.id}`;
  const set = (next: MapNas, key?: string) => patch({ nas: next }, key);
  const entry = (n: number) => nas.bay_disks.find((b) => b.bay === n);
  const change = (n: number, part: { label?: string; disk?: string }) => {
    const now = { bay: n, label: "", disk: "", ...entry(n), ...part };
    const rest = nas.bay_disks.filter((b) => b.bay !== n);
    set(
      {
        ...nas,
        bay_disks:
          now.label === "" && now.disk === "" ? rest : [...rest, now].sort((a, b) => a.bay - b.bay),
      },
      `${box.id}:bay${n}`,
    );
  };
  return (
    <fieldset className="flex flex-col gap-2 border-t border-line pt-3">
      <legend className={labelText}>Drive bays</legend>
      <NumField
        label="Bays"
        value={nas.bays}
        min={1}
        max={16}
        onCommit={(bays) => set({ bays, bay_disks: nas.bay_disks.filter((b) => b.bay <= bays) })}
      />
      <p className="text-xs text-muted">
        {box.instance_id
          ? disks.length > 0
            ? "Pick the disk of each bay from the SMART exporter on this device, or type its device name or serial number."
            : "No SMART disks reported for this device. Type a device name or serial number, matched once the exporter reports it."
          : "Link a device to pick its SMART disks. A bay with no disk is drawn empty."}
      </p>
      <datalist id={listId}>
        {disks.map((d) => (
          <option key={d.device} value={d.serial || d.device}>
            {[d.device, d.model].filter(Boolean).join(" · ")}
          </option>
        ))}
      </datalist>
      {Array.from({ length: nas.bays }, (_, i) => i + 1).map((n) => (
        <div key={n} className="grid grid-cols-[2rem_5rem_1fr] items-center gap-1.5">
          <b className="text-xs">{n}</b>
          <input
            className={input}
            aria-label={`Bay ${n} name`}
            placeholder="Name"
            maxLength={16}
            value={entry(n)?.label ?? ""}
            onChange={(e) => change(n, { label: e.target.value })}
          />
          <input
            className={input}
            aria-label={`Bay ${n} disk`}
            placeholder="Disk (empty = empty bay)"
            list={listId}
            maxLength={64}
            value={entry(n)?.disk ?? ""}
            onChange={(e) => change(n, { disk: e.target.value })}
          />
        </div>
      ))}
    </fieldset>
  );
}
