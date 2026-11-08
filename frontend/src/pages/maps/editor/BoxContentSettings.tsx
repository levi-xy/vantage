// The inspector's fields for what a box says about itself: role, the
// identity lines and the sections (tiles / services / list) with their items. Every field is
// plain text; the caps are the backend's (map_document.py).

import type { Instance, MapBox, MapItem, MapSection } from "../../../api";
import { buttonSecondary, input } from "../../../components/styles";
import { FIGURES } from "../figures";
import { Field, ColorPicker, labelText } from "./fields";
import { IconPicker } from "./IconPicker";
import {
  addItem,
  addSection,
  MAX_ITEMS,
  MAX_SECTIONS,
  moveItem,
  moveSection,
  patchItem,
  patchSection,
  removeItem,
  removeSection,
} from "./sectionOps";

type Patch = (patch: Record<string, unknown>, key?: string) => void;

function Row({
  label,
  value,
  max,
  onChange,
}: {
  label: string;
  value: string;
  max: number;
  onChange: (v: string) => void;
}) {
  return (
    <Field label={label}>
      <input
        className={input}
        value={value}
        maxLength={max}
        onChange={(e) => onChange(e.target.value)}
      />
    </Field>
  );
}

const small = `${buttonSecondary} px-2 py-0.5 text-xs`;

function ItemEditor({
  item,
  index,
  count,
  figures,
  set,
  move,
  remove,
}: {
  item: MapItem;
  index: number;
  count: number;
  figures: readonly { id: string; label: string }[];
  set: (patch: Partial<Omit<MapItem, "id">>) => void;
  move: (by: number) => void;
  remove: () => void;
}) {
  const n = index + 1;
  return (
    <div
      className="flex flex-col gap-1.5 rounded-md border border-line p-2"
      data-item-editor={item.id}
    >
      <div className="flex gap-1.5">
        <input
          className={input}
          aria-label={`Item ${n} label`}
          placeholder="Label"
          value={item.label}
          maxLength={32}
          onChange={(e) => set({ label: e.target.value })}
        />
        <input
          className={input}
          aria-label={`Item ${n} value`}
          placeholder={item.figure ? "(live)" : "Value"}
          value={item.value}
          maxLength={32}
          disabled={item.figure !== null}
          onChange={(e) => set({ value: e.target.value })}
        />
      </div>
      <input
        className={input}
        aria-label={`Item ${n} sub line`}
        placeholder="Small text under it"
        value={item.sub}
        maxLength={48}
        onChange={(e) => set({ sub: e.target.value })}
      />
      <select
        className={input}
        aria-label={`Item ${n} live figure`}
        value={item.figure ?? ""}
        onChange={(e) => set({ figure: e.target.value || null })}
      >
        <option value="">Typed value (no live figure)</option>
        {figures.map((f) => (
          <option key={f.id} value={f.id}>
            Live: {f.label}
          </option>
        ))}
        {item.figure && !figures.some((f) => f.id === item.figure) && (
          <option value={item.figure}>{item.figure} (not on this device)</option>
        )}
      </select>
      <ColorPicker value={item.color} noneLabel="No colour" onChange={(color) => set({ color })} />
      <details>
        <summary className="cursor-pointer text-xs text-muted">
          Icon{item.icon ? " (set)" : ""}
        </summary>
        <div className="mt-1.5 flex flex-col gap-1.5">
          <IconPicker value={item.icon ?? "generic"} onChange={(icon) => set({ icon })} />
          {item.icon && (
            <button type="button" className={small} onClick={() => set({ icon: null })}>
              No icon
            </button>
          )}
        </div>
      </details>
      <div className="flex gap-1.5">
        <button
          type="button"
          aria-label={`Move item ${n} up`}
          className={small}
          disabled={index === 0}
          onClick={() => move(-1)}
        >
          ↑
        </button>
        <button
          type="button"
          aria-label={`Move item ${n} down`}
          className={small}
          disabled={index === count - 1}
          onClick={() => move(1)}
        >
          ↓
        </button>
        <button type="button" aria-label={`Remove item ${n}`} className={small} onClick={remove}>
          ×
        </button>
      </div>
    </div>
  );
}

export function SectionsEditor({
  sections,
  instance,
  set,
}: {
  sections: readonly MapSection[];
  instance: Instance | undefined;
  set: (sections: MapSection[]) => void;
}) {
  const figures = instance ? (FIGURES[instance.type_id] ?? []) : [];
  return (
    <fieldset className="flex flex-col gap-2">
      <legend className={labelText}>Sections (up to {MAX_SECTIONS})</legend>
      {sections.map((s, si) => (
        <div
          key={s.id}
          data-section-editor={s.id}
          className="flex flex-col gap-2 rounded-md border border-line p-2"
        >
          <div className="flex gap-1.5">
            <input
              className={input}
              aria-label={`Section ${si + 1} title`}
              placeholder="Title"
              value={s.title}
              maxLength={32}
              onChange={(e) => set(patchSection(sections, s.id, { title: e.target.value }))}
            />
            <select
              className={input}
              aria-label={`Section ${si + 1} kind`}
              value={s.kind}
              onChange={(e) =>
                set(patchSection(sections, s.id, { kind: e.target.value as MapSection["kind"] }))
              }
            >
              <option value="tiles">Tiles</option>
              <option value="services">Services</option>
              <option value="list">List</option>
            </select>
          </div>
          {s.kind !== "list" && (
            <Field label="Columns">
              <select
                className={input}
                aria-label={`Section ${si + 1} columns`}
                value={s.columns}
                onChange={(e) =>
                  set(
                    patchSection(sections, s.id, {
                      columns: Number(e.target.value) as MapSection["columns"],
                    }),
                  )
                }
              >
                {[1, 2, 3, 4].map((c) => (
                  <option key={c} value={c}>
                    {c}
                  </option>
                ))}
              </select>
            </Field>
          )}
          {s.items.map((item, ii) => (
            <ItemEditor
              key={item.id}
              item={item}
              index={ii}
              count={s.items.length}
              figures={figures}
              set={(patch) => set(patchItem(sections, s.id, item.id, patch))}
              move={(by) => set(moveItem(sections, s.id, ii, by))}
              remove={() => set(removeItem(sections, s.id, item.id))}
            />
          ))}
          <button
            type="button"
            className={buttonSecondary}
            disabled={s.items.length >= MAX_ITEMS}
            onClick={() => set(addItem(sections, s.id))}
          >
            Add item
          </button>
          <div className="flex gap-1.5">
            <button
              type="button"
              aria-label={`Move section ${si + 1} up`}
              className={small}
              disabled={si === 0}
              onClick={() => set(moveSection(sections, si, -1))}
            >
              ↑
            </button>
            <button
              type="button"
              aria-label={`Move section ${si + 1} down`}
              className={small}
              disabled={si === sections.length - 1}
              onClick={() => set(moveSection(sections, si, 1))}
            >
              ↓
            </button>
            <button
              type="button"
              aria-label={`Remove section ${si + 1}`}
              className={small}
              onClick={() => set(removeSection(sections, s.id))}
            >
              Remove section
            </button>
          </div>
        </div>
      ))}
      <button
        type="button"
        className={buttonSecondary}
        disabled={sections.length >= MAX_SECTIONS}
        onClick={() => set(addSection(sections))}
      >
        Add section
      </button>
      {instance && figures.length === 0 && (
        <p className="text-xs text-muted">This device type has no live figures to pick.</p>
      )}
      {!instance && <p className="text-xs text-muted">Link a device to give items live figures.</p>}
    </fieldset>
  );
}

/** Role, identity lines and sections of one box (shape and panel are on the "Look and ports" tab). */
export function BoxContentSettings({
  box,
  instance,
  patch,
}: {
  box: MapBox;
  instance: Instance | undefined;
  patch: Patch;
}) {
  const shape = box.shape ?? "card";
  return (
    <>
      {shape !== "cloud" && (
        <>
          <Row
            label="Role (after the name)"
            value={box.role ?? ""}
            max={32}
            onChange={(role) => patch({ role }, `${box.id}:role`)}
          />
          <Row
            label="Model"
            value={box.model ?? ""}
            max={64}
            onChange={(model) => patch({ model }, `${box.id}:model`)}
          />
          <Row
            label="Address"
            value={box.address ?? ""}
            max={64}
            onChange={(address) => patch({ address }, `${box.id}:address`)}
          />
          <Row
            label="Version"
            value={box.version ?? ""}
            max={64}
            onChange={(version) => patch({ version }, `${box.id}:version`)}
          />
          {instance && (
            <p className="text-xs text-muted">
              Empty model and address are filled in from the linked device.
            </p>
          )}
          <SectionsEditor
            sections={box.sections ?? []}
            instance={instance}
            set={(sections) => patch({ sections })}
          />
        </>
      )}
    </>
  );
}
