// A frame's guest groups in the inspector: titled columns the guests are sorted into. Drag a
// guest onto another group (or use its "Group" select, for keyboard and touch); guests in no
// group fall into "Other" on the drawing.

import type { MapFrame, MapGuestGroup } from "../../../api";
import { useProxmoxGuests } from "../../../cards/proxmox-guests/useProxmoxGuests";
import { buttonSecondary, input } from "../../../components/styles";
import { guestTiles } from "../guests";
import type { GuestTile } from "../guests";
import { labelText } from "./fields";
import {
  addGroup,
  assignGuest,
  groupOf,
  MAX_GROUPS,
  moveGroup,
  removeGroup,
  renameGroup,
} from "./frameOps";

const GUEST_KEY = "application/x-vantage-guest";
const small = `${buttonSecondary} px-2 py-0.5 text-xs`;

type SetGroups = (groups: MapGuestGroup[], key?: string) => void;

function GuestRow({
  tile,
  groups,
  value,
  set,
}: {
  tile: GuestTile;
  groups: readonly MapGuestGroup[];
  value: string | null;
  set: SetGroups;
}) {
  return (
    <div
      draggable
      data-guest-row={tile.key}
      onDragStart={(e) => e.dataTransfer.setData(GUEST_KEY, tile.key)}
      className="flex cursor-grab items-center gap-1.5 rounded border border-line bg-inset px-1.5 py-1 text-xs"
    >
      <span className="min-w-0 flex-1 truncate" title={tile.name}>
        {tile.name}
      </span>
      {tile.ref && <span className="shrink-0 text-muted">{tile.ref}</span>}
      <select
        className="w-24 shrink-0 rounded border border-line bg-panel px-1 py-0.5 text-xs"
        aria-label={`Group of ${tile.name}`}
        value={value ?? ""}
        onChange={(e) => set(assignGuest(groups, tile.key, e.target.value || null))}
      >
        <option value="">Other</option>
        {groups.map((g) => (
          <option key={g.id} value={g.id}>
            {g.title || g.id}
          </option>
        ))}
      </select>
    </div>
  );
}

function Drop({
  id,
  groups,
  set,
  children,
}: {
  id: string | null;
  groups: readonly MapGuestGroup[];
  set: SetGroups;
  children: React.ReactNode;
}) {
  return (
    <div
      className="flex flex-col gap-1 rounded-md border border-dashed border-line p-1.5"
      onDragOver={(e) => e.preventDefault()}
      onDrop={(e) => {
        const key = e.dataTransfer.getData(GUEST_KEY);
        if (!key) return;
        e.preventDefault();
        e.stopPropagation();
        set(assignGuest(groups, key, id));
      }}
    >
      {children}
    </div>
  );
}

function GroupsBody({
  groups,
  tiles,
  set,
}: {
  groups: readonly MapGuestGroup[];
  tiles: GuestTile[];
  set: SetGroups;
}) {
  const other = tiles.filter((t) => groupOf(groups, t.key) === null);
  return (
    <fieldset className="flex flex-col gap-2">
      <legend className={labelText}>Guest groups (up to {MAX_GROUPS})</legend>
      {groups.map((g, gi) => (
        <div key={g.id} data-group-editor={g.id} className="flex flex-col gap-1.5">
          <div className="flex gap-1.5">
            <input
              className={input}
              aria-label={`Group ${gi + 1} title`}
              value={g.title}
              maxLength={32}
              onChange={(e) => set(renameGroup(groups, g.id, e.target.value), `group:${g.id}`)}
            />
            <button
              type="button"
              aria-label={`Move group ${gi + 1} up`}
              className={small}
              disabled={gi === 0}
              onClick={() => set(moveGroup(groups, gi, -1))}
            >
              ↑
            </button>
            <button
              type="button"
              aria-label={`Move group ${gi + 1} down`}
              className={small}
              disabled={gi === groups.length - 1}
              onClick={() => set(moveGroup(groups, gi, 1))}
            >
              ↓
            </button>
            <button
              type="button"
              aria-label={`Remove group ${gi + 1}`}
              className={small}
              onClick={() => set(removeGroup(groups, g.id))}
            >
              ×
            </button>
          </div>
          <Drop id={g.id} groups={groups} set={set}>
            {tiles
              .filter((t) => groupOf(groups, t.key) === g.id)
              .map((t) => (
                <GuestRow key={t.key} tile={t} groups={groups} value={g.id} set={set} />
              ))}
            {!tiles.some((t) => groupOf(groups, t.key) === g.id) && (
              <span className="text-xs text-muted">Drag guests here.</span>
            )}
          </Drop>
        </div>
      ))}
      {groups.length > 0 && (
        <div data-group-editor="other" className="flex flex-col gap-1.5">
          <span className="text-xs font-medium text-muted">Other (in no group)</span>
          <Drop id={null} groups={groups} set={set}>
            {other.map((t) => (
              <GuestRow key={t.key} tile={t} groups={groups} value={null} set={set} />
            ))}
            {other.length === 0 && <span className="text-xs text-muted">Empty, so hidden.</span>}
          </Drop>
        </div>
      )}
      <button
        type="button"
        className={buttonSecondary}
        disabled={groups.length >= MAX_GROUPS}
        onClick={() => set(addGroup(groups))}
      >
        Add group
      </button>
    </fieldset>
  );
}

function ProxmoxGroups({
  frame,
  instanceId,
  set,
}: {
  frame: MapFrame;
  instanceId: string;
  set: SetGroups;
}) {
  const { guests } = useProxmoxGuests(instanceId);
  return <GroupsBody groups={frame.groups} tiles={guestTiles(guests, frame.guests)} set={set} />;
}

export function FrameGroups({
  frame,
  proxmoxId,
  set,
}: {
  frame: MapFrame;
  /** The frame's Proxmox instance (its guests fill the groups), or null. */
  proxmoxId: string | null;
  set: SetGroups;
}) {
  return proxmoxId ? (
    <ProxmoxGroups frame={frame} instanceId={proxmoxId} set={set} />
  ) : (
    <GroupsBody groups={frame.groups} tiles={guestTiles([], frame.guests)} set={set} />
  );
}
