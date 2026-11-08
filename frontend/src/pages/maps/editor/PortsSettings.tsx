// A box's port strip in the inspector: none, the device's own interfaces, or ports typed by
// hand; row or front-panel layout; each port's VLANs (untagged and tagged) from the map's
// VLAN list. On a device strip the VLANs are overrides stored by interface id.

import type { Instance, MapBox, MapDocument, MapPort, MapPorts } from "../../../api";
import { buttonSecondary, input } from "../../../components/styles";
import { hasPorts } from "../devicePorts";
import type { MapLive } from "../mapLive";
import { Check, CommitText, labelText } from "./fields";
import { MAX_PORTS, MAX_TAGGED, newPortId, renamePort, setBoxPorts } from "./mapOps";
import { patchElement } from "./model";

type Edit = (next: MapDocument) => void;

const NO_VLANS: MapPort["vlans"] = { untagged: null, tagged: [] };

function PortVlans({
  name,
  vlans,
  choices,
  onChange,
}: {
  name: string;
  vlans: MapPort["vlans"];
  choices: MapDocument["vlans"];
  onChange: (v: MapPort["vlans"]) => void;
}) {
  if (choices.length === 0) {
    return <p className="text-xs text-muted">Add VLANs with the VLANs button to set them here.</p>;
  }
  return (
    <div className="flex flex-col gap-1.5">
      <select
        className={input}
        aria-label={`${name} untagged VLAN`}
        value={vlans.untagged ?? ""}
        onChange={(e) => {
          const untagged = e.target.value || null;
          onChange({ untagged, tagged: vlans.tagged.filter((t) => t !== untagged) });
        }}
      >
        <option value="">No untagged VLAN</option>
        {choices.map((v) => (
          <option key={v.id} value={v.id}>
            {v.id} · {v.name}
          </option>
        ))}
      </select>
      <span className={labelText}>Tagged</span>
      {choices.map((v) => {
        const on = vlans.tagged.includes(v.id);
        return (
          <Check
            key={v.id}
            label={`${name} tagged VLAN ${v.id}`}
            checked={on}
            disabled={v.id === vlans.untagged || (!on && vlans.tagged.length >= MAX_TAGGED)}
            onChange={(yes) =>
              onChange({
                untagged: vlans.untagged,
                tagged: yes ? [...vlans.tagged, v.id] : vlans.tagged.filter((t) => t !== v.id),
              })
            }
          />
        );
      })}
    </div>
  );
}

const vlanSummary = (v: MapPort["vlans"]) =>
  [
    v.untagged ? `untagged ${v.untagged}` : "",
    v.tagged.length ? `tagged ${v.tagged.join(", ")}` : "",
  ]
    .filter(Boolean)
    .join(" · ");

export function PortsSettings({
  doc,
  box,
  instance,
  live,
  edit,
}: {
  doc: MapDocument;
  box: MapBox;
  instance: Instance | undefined;
  live: MapLive;
  edit: Edit;
}) {
  const strip = box.ports;
  const source = strip?.source ?? "none";
  const setPorts = (ports: MapPorts) => edit(patchElement(doc, box.id, { ports }));

  const choose = (v: string) => {
    if (v === "none") return edit(setBoxPorts(doc, box.id, null));
    const kind = v as "device" | "manual";
    edit(
      setBoxPorts(doc, box.id, {
        source: kind,
        layout: strip?.layout ?? "row",
        ports: strip?.source === kind ? strip.ports : [],
      }),
    );
  };

  return (
    <fieldset className="flex flex-col gap-2 border-t border-line pt-3">
      <legend className={labelText}>Ports</legend>
      <select
        className={input}
        aria-label="Port strip"
        value={source}
        onChange={(e) => choose(e.target.value)}
      >
        <option value="none">No ports</option>
        <option value="device" disabled={!instance || !hasPorts(instance)}>
          From the device
        </option>
        <option value="manual">Typed by hand</option>
      </select>
      {strip && (
        <select
          className={input}
          aria-label="Strip layout"
          value={strip.layout}
          onChange={(e) => setPorts({ ...strip, layout: e.target.value as "row" | "panel" })}
        >
          <option value="row">In a row</option>
          <option value="panel">As a front panel</option>
        </select>
      )}
      {strip?.source === "manual" && <ManualPorts doc={doc} box={box} strip={strip} edit={edit} />}
      {strip?.source === "device" && (
        <DevicePorts doc={doc} box={box} strip={strip} live={live} edit={edit} />
      )}
    </fieldset>
  );
}

function ManualPorts({
  doc,
  box,
  strip,
  edit,
}: {
  doc: MapDocument;
  box: MapBox;
  strip: MapPorts;
  edit: Edit;
}) {
  const ports = strip.ports;
  const update = (list: MapPort[]) =>
    edit(patchElement(doc, box.id, { ports: { ...strip, ports: list } }));
  const change = (i: number, patch: Partial<MapPort>) =>
    update(ports.map((p, k) => (k === i ? { ...p, ...patch } : p)));
  const move = (i: number, by: number) => {
    const list = [...ports];
    const [p] = list.splice(i, 1);
    if (p) list.splice(i + by, 0, p);
    update(list);
  };
  return (
    <div className="flex flex-col gap-2">
      {ports.map((p, i) => {
        const name = `Port ${i + 1}`;
        return (
          <div key={p.id} className="flex flex-col gap-1.5 rounded-md border border-line p-2">
            <div className="grid grid-cols-2 gap-1.5">
              <CommitText
                label={`${name} id`}
                value={p.id}
                maxLength={64}
                accept={(v) => v.trim() !== "" && !ports.some((q) => q.id === v)}
                onCommit={(v) => edit(renamePort(doc, box.id, p.id, v))}
              />
              <CommitText
                label={`${name} label`}
                value={p.label}
                maxLength={16}
                accept={() => true}
                onCommit={(v) => change(i, { label: v })}
              />
            </div>
            <select
              className={input}
              aria-label={`${name} type`}
              value={p.kind}
              onChange={(e) => change(i, { kind: e.target.value as "rj45" | "sfp" })}
            >
              <option value="rj45">RJ45</option>
              <option value="sfp">SFP</option>
            </select>
            <details>
              <summary className="cursor-pointer text-xs text-muted">
                VLANs{vlanSummary(p.vlans) ? `: ${vlanSummary(p.vlans)}` : ""}
              </summary>
              <PortVlans
                name={name}
                vlans={p.vlans}
                choices={doc.vlans}
                onChange={(vlans) => change(i, { vlans })}
              />
            </details>
            <div className="flex gap-1.5">
              <button
                type="button"
                aria-label={`Move ${name} up`}
                className={buttonSecondary}
                disabled={i === 0}
                onClick={() => move(i, -1)}
              >
                ↑
              </button>
              <button
                type="button"
                aria-label={`Move ${name} down`}
                className={buttonSecondary}
                disabled={i === ports.length - 1}
                onClick={() => move(i, 1)}
              >
                ↓
              </button>
              <button
                type="button"
                aria-label={`Remove ${name}`}
                className={buttonSecondary}
                onClick={() =>
                  edit(
                    setBoxPorts(doc, box.id, { ...strip, ports: ports.filter((_, k) => k !== i) }),
                  )
                }
              >
                ×
              </button>
            </div>
          </div>
        );
      })}
      <button
        type="button"
        className={buttonSecondary}
        disabled={ports.length >= MAX_PORTS}
        onClick={() =>
          update([...ports, { id: newPortId(ports), label: "", kind: "rj45", vlans: NO_VLANS }])
        }
      >
        Add port
      </button>
    </div>
  );
}

function DevicePorts({
  doc,
  box,
  strip,
  live,
  edit,
}: {
  doc: MapDocument;
  box: MapBox;
  strip: MapPorts;
  live: MapLive;
  edit: Edit;
}) {
  const device = live.ports(box.instance_id) ?? [];
  const overrides = new Map(strip.ports.map((p) => [p.id, p] as const));
  const setVlans = (id: string, vlans: MapPort["vlans"]) => {
    const old = overrides.get(id);
    const empty = vlans.untagged === null && vlans.tagged.length === 0;
    const rest = strip.ports.filter((p) => p.id !== id);
    const next: MapPort[] =
      empty && !old?.label
        ? rest
        : strip.ports.some((p) => p.id === id)
          ? strip.ports.map((p) => (p.id === id ? { ...p, vlans } : p))
          : [...rest, { id, label: "", kind: "rj45", vlans }];
    edit(patchElement(doc, box.id, { ports: { ...strip, ports: next } }));
  };
  if (device.length === 0) {
    return <p className="text-xs text-muted">The device&apos;s interfaces haven&apos;t loaded.</p>;
  }
  return (
    <div className="flex flex-col gap-1">
      <span className={labelText}>VLANs per interface (optional)</span>
      {device.map((d) => {
        const vlans = overrides.get(d.id)?.vlans ?? NO_VLANS;
        const room = overrides.has(d.id) || strip.ports.length < MAX_PORTS;
        return (
          <details key={d.id}>
            <summary className="cursor-pointer text-sm">
              {d.name}
              <span className="text-xs text-muted">
                {vlanSummary(vlans) ? ` · ${vlanSummary(vlans)}` : ""}
              </span>
            </summary>
            {room ? (
              <PortVlans
                name={d.name}
                vlans={vlans}
                choices={doc.vlans}
                onChange={(v) => setVlans(d.id, v)}
              />
            ) : (
              <p className="text-xs text-muted">A strip holds at most {MAX_PORTS} ports.</p>
            )}
          </details>
        );
      })}
    </div>
  );
}
