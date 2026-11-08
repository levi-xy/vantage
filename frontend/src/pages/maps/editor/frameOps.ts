// The frame groups editor's pure operations (caps as in backend/app/api/map_document.py).

import type { MapGuestGroup } from "../../../api";

export const MAX_GROUPS = 12;
export const MAX_MEMBERS = 200;

export function addGroup(groups: readonly MapGuestGroup[]): MapGuestGroup[] {
  if (groups.length >= MAX_GROUPS) return [...groups];
  const used = new Set(groups.map((g) => g.id));
  let n = 1;
  while (used.has(`g${n}`)) n++;
  return [...groups, { id: `g${n}`, title: "New group", members: [] }];
}

export function renameGroup(
  groups: readonly MapGuestGroup[],
  id: string,
  title: string,
): MapGuestGroup[] {
  return groups.map((g) => (g.id === id ? { ...g, title } : g));
}

export function removeGroup(groups: readonly MapGuestGroup[], id: string): MapGuestGroup[] {
  return groups.filter((g) => g.id !== id);
}

export function moveGroup(
  groups: readonly MapGuestGroup[],
  from: number,
  by: number,
): MapGuestGroup[] {
  const to = from + by;
  if (from < 0 || from >= groups.length || to < 0 || to >= groups.length) return [...groups];
  const out = [...groups];
  const [one] = out.splice(from, 1);
  out.splice(to, 0, one as MapGuestGroup);
  return out;
}

/** Put a guest (by tile key) in a group, or in none (`null`: it falls into "Other"). */
export function assignGuest(
  groups: readonly MapGuestGroup[],
  key: string,
  groupId: string | null,
): MapGuestGroup[] {
  return groups.map((g) => {
    const without = g.members.filter((m) => m !== key);
    if (g.id !== groupId) return { ...g, members: without };
    return { ...g, members: [...without, key].slice(0, MAX_MEMBERS) };
  });
}

/** The group that lists this guest first (null: none, so "Other"). */
export function groupOf(groups: readonly MapGuestGroup[], key: string): string | null {
  return groups.find((g) => g.members.includes(key))?.id ?? null;
}
