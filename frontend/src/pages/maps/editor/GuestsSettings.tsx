// A frame's tiles in the inspector: the Proxmox guests of its device (show or hide, move up
// or down; stored by vmid in `guests.hidden` and `guests.order`) and tiles typed by hand for
// things that aren't Proxmox guests.

import type { MapFrame, MapGuests } from "../../../api";
import { useProxmoxGuests } from "../../../cards/proxmox-guests/useProxmoxGuests";
import { buttonSecondary, input } from "../../../components/styles";
import { orderGuests } from "../guests";
import { Check, labelText } from "./fields";
import { MAX_GUEST_LIST } from "./mapOps";

const MAX_MANUAL = 50;
type SetGuests = (guests: MapGuests, key?: string) => void;

export function ProxmoxGuestList({
  guests,
  instanceId,
  set,
}: {
  guests: MapGuests;
  instanceId: string;
  set: SetGuests;
}) {
  const all = orderGuests(useProxmoxGuests(instanceId).guests, guests.order);
  const move = (i: number, by: number) => {
    const ids = all.map((g) => g.vmid);
    const [id] = ids.splice(i, 1);
    if (id !== undefined) ids.splice(i + by, 0, id);
    set({ ...guests, order: ids.slice(0, MAX_GUEST_LIST) });
  };
  const show = (vmid: string, on: boolean) =>
    set({
      ...guests,
      hidden: on
        ? guests.hidden.filter((h) => h !== vmid)
        : guests.hidden.includes(vmid) || guests.hidden.length >= MAX_GUEST_LIST
          ? guests.hidden
          : [...guests.hidden, vmid],
    });
  return (
    <fieldset className="flex flex-col gap-1.5">
      <legend className={labelText}>Guests from Proxmox</legend>
      {all.length === 0 && <p className="text-xs text-muted">No guests found yet.</p>}
      {all.map((g, i) => (
        <div key={g.vmid} className="flex items-center gap-1.5">
          <Check
            label={`Show ${g.name}`}
            checked={!guests.hidden.includes(g.vmid)}
            onChange={(on) => show(g.vmid, on)}
          />
          <span className="ml-auto text-xs text-muted">{g.vmid}</span>
          <button
            type="button"
            aria-label={`Move ${g.name} up`}
            className={buttonSecondary}
            disabled={i === 0}
            onClick={() => move(i, -1)}
          >
            ↑
          </button>
          <button
            type="button"
            aria-label={`Move ${g.name} down`}
            className={buttonSecondary}
            disabled={i === all.length - 1}
            onClick={() => move(i, 1)}
          >
            ↓
          </button>
        </div>
      ))}
    </fieldset>
  );
}

export function ManualTiles({ frame, set }: { frame: MapFrame; set: SetGuests }) {
  const g = frame.guests;
  const change = (id: string, p: { label?: string; subtitle?: string }, key: string) =>
    set({ ...g, manual: g.manual.map((m) => (m.id === id ? { ...m, ...p } : m)) }, key);
  const add = () => {
    const used = new Set(g.manual.map((m) => m.id));
    let n = 1;
    while (used.has(`m${n}`)) n++;
    set({ ...g, manual: [...g.manual, { id: `m${n}`, label: "New tile", subtitle: "" }] });
  };
  return (
    <fieldset className="flex flex-col gap-2">
      <legend className={labelText}>Tiles of your own</legend>
      {g.manual.map((m, i) => (
        <div key={m.id} className="flex flex-col gap-1.5 rounded-md border border-line p-2">
          <input
            className={input}
            aria-label={`Tile ${i + 1} label`}
            value={m.label}
            maxLength={64}
            onChange={(e) => change(m.id, { label: e.target.value }, `${frame.id}:${m.id}:label`)}
          />
          <input
            className={input}
            aria-label={`Tile ${i + 1} subtitle`}
            value={m.subtitle}
            maxLength={64}
            onChange={(e) =>
              change(m.id, { subtitle: e.target.value }, `${frame.id}:${m.id}:subtitle`)
            }
          />
          <button
            type="button"
            aria-label={`Remove tile ${i + 1}`}
            className={buttonSecondary}
            onClick={() => set({ ...g, manual: g.manual.filter((x) => x.id !== m.id) })}
          >
            Remove tile
          </button>
        </div>
      ))}
      <button
        type="button"
        className={buttonSecondary}
        disabled={g.manual.length >= MAX_MANUAL}
        onClick={add}
      >
        Add tile
      </button>
    </fieldset>
  );
}
