// The inspector of a selected cable: its two ends (element and port), speed, line style, role
// and VLAN, colour, label, live rate and route.

import type { Instance, MapBox, MapCable, MapCableEnd, MapDocument, MapFrame } from "../../../api";
import { buttonSecondary, input } from "../../../components/styles";
import { SPEED_ORDER, speedLabel } from "../geometry";
import { STYLE_NAME } from "../legend";
import type { MapLive } from "../mapLive";
import { Check, ColorPicker, Field, labelText } from "./fields";

type Patch = (patch: Record<string, unknown>, key?: string) => void;

function elementName(el: MapBox | MapFrame, instances: readonly Instance[]): string {
  return el.label || instances.find((i) => i.id === el.instance_id)?.name || "Unnamed";
}

function EndPicker({
  title,
  end,
  otherElement,
  doc,
  instances,
  live,
  onChange,
}: {
  title: string;
  end: MapCableEnd;
  otherElement: string;
  doc: MapDocument;
  instances: readonly Instance[];
  live: MapLive;
  onChange: (end: MapCableEnd) => void;
}) {
  const targets = doc.elements.filter(
    (e): e is MapBox | MapFrame =>
      (e.kind === "box" || e.kind === "frame") && (e.id !== otherElement || e.id === end.element),
  );
  const owner = targets.find((e) => e.id === end.element);
  const strip = owner?.kind === "box" ? owner.ports : null;
  const ports: { id: string; name: string }[] =
    owner?.kind !== "box" || !strip
      ? []
      : strip.source === "manual"
        ? strip.ports.map((p) => ({ id: p.id, name: p.label || p.id }))
        : (live.ports(owner.instance_id) ?? []).map((p) => ({ id: p.id, name: p.name }));
  if (end.port !== null && !ports.some((p) => p.id === end.port)) {
    ports.push({ id: end.port, name: end.port });
  }
  return (
    <fieldset className="flex flex-col gap-1.5">
      <legend className={labelText}>{title}</legend>
      <select
        className={input}
        aria-label={`${title} element`}
        value={end.element}
        onChange={(e) => onChange({ element: e.target.value, port: null })}
      >
        {targets.map((e) => (
          <option key={e.id} value={e.id}>
            {elementName(e, instances)}
          </option>
        ))}
      </select>
      {strip ? (
        <select
          className={input}
          aria-label={`${title} port`}
          value={end.port ?? ""}
          onChange={(e) => onChange({ element: end.element, port: e.target.value || null })}
        >
          <option value="">Edge of the box</option>
          {ports.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}
            </option>
          ))}
        </select>
      ) : (
        <p className="text-xs text-muted">
          Meets the edge of the box. Add a port strip to the box to pick a port.
        </p>
      )}
    </fieldset>
  );
}

export function CableSettings({
  cable,
  doc,
  instances,
  live,
  patch,
}: {
  cable: MapCable;
  doc: MapDocument;
  instances: readonly Instance[];
  live: MapLive;
  patch: Patch;
}) {
  return (
    <>
      <EndPicker
        title="From"
        end={cable.from}
        otherElement={cable.to.element}
        doc={doc}
        instances={instances}
        live={live}
        onChange={(from) => patch({ from })}
      />
      <EndPicker
        title="To"
        end={cable.to}
        otherElement={cable.from.element}
        doc={doc}
        instances={instances}
        live={live}
        onChange={(to) => patch({ to })}
      />
      <Field label="Speed">
        <select
          className={input}
          value={cable.speed ?? ""}
          onChange={(e) => patch({ speed: e.target.value || null })}
        >
          <option value="">Not set</option>
          {SPEED_ORDER.map((s) => (
            <option key={s} value={s}>
              {speedLabel(s)}
            </option>
          ))}
        </select>
        <span className="mt-1 block text-xs text-muted">
          Sets the line&apos;s thickness. Filled in from the port when it reports a speed.
        </span>
      </Field>
      <Field label="Line">
        <select
          className={input}
          value={cable.style}
          onChange={(e) => patch({ style: e.target.value })}
        >
          {(Object.keys(STYLE_NAME) as (keyof typeof STYLE_NAME)[]).map((s) => (
            <option key={s} value={s}>
              {STYLE_NAME[s]}
            </option>
          ))}
        </select>
      </Field>
      <Field label="Role">
        <select
          className={input}
          value={cable.role ?? ""}
          onChange={(e) => {
            const role = e.target.value || null;
            patch({ role, vlan: role === "access" ? cable.vlan : null });
          }}
        >
          <option value="">None</option>
          <option value="access">Access (one VLAN)</option>
          <option value="trunk">Trunk (several VLANs)</option>
        </select>
      </Field>
      {cable.role === "access" && (
        <Field label="VLAN">
          <select
            className={input}
            value={cable.vlan ?? ""}
            onChange={(e) => patch({ vlan: e.target.value || null })}
          >
            <option value="">No VLAN</option>
            {doc.vlans.map((v) => (
              <option key={v.id} value={v.id}>
                {v.id} · {v.name}
              </option>
            ))}
          </select>
          {doc.vlans.length === 0 && (
            <span className="mt-1 block text-xs text-muted">
              Add VLANs with the VLANs button above the map.
            </span>
          )}
        </Field>
      )}
      <div>
        <span className={labelText}>Colour</span>
        <ColorPicker
          value={cable.color}
          noneLabel={cable.role === "access" && cable.vlan ? "VLAN colour" : "No colour"}
          onChange={(color) => patch({ color })}
        />
        <span className="mt-1 block text-xs text-muted">Red is kept for problems.</span>
      </div>
      <Field label="Label">
        <input
          className={input}
          value={cable.label}
          maxLength={64}
          onChange={(e) => patch({ label: e.target.value }, `${cable.id}:label`)}
        />
      </Field>
      <Check
        label="Show the rate next to the label"
        checked={cable.show_rate}
        onChange={(v) => patch({ show_rate: v })}
      />
      <div>
        <span className={labelText}>Route</span>
        {/* TODO(maintainer): the map schema has no field for a straight route, so cables are
            always drawn at right angles (auto, or through the bends). Add one to allow it. */}
        <p className="text-sm">Right angles</p>
        <p className="text-xs text-muted">
          Drag the cable to add a bend; drag a bend to move it; double-click a bend to remove it.
        </p>
        {cable.bends.length > 0 && (
          <button
            type="button"
            className={`${buttonSecondary} mt-1.5`}
            onClick={() => patch({ bends: [] })}
          >
            Remove {cable.bends.length === 1 ? "the bend" : `all ${cable.bends.length} bends`}
          </button>
        )}
      </div>
    </>
  );
}
