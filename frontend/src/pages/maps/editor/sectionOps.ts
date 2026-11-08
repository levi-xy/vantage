// The sections editor's pure operations (caps as in backend/app/api/map_document.py).

import type { MapItem, MapSection } from "../../../api";

export const MAX_SECTIONS = 4;
export const MAX_ITEMS = 12;

function freshId(used: Iterable<string>, prefix: string): string {
  const taken = new Set(used);
  for (let n = 1; ; n++) {
    if (!taken.has(`${prefix}${n}`)) return `${prefix}${n}`;
  }
}

export function addSection(sections: readonly MapSection[]): MapSection[] {
  if (sections.length >= MAX_SECTIONS) return [...sections];
  const section: MapSection = {
    id: freshId(
      sections.map((s) => s.id),
      "s",
    ),
    title: "",
    kind: "tiles",
    columns: 2,
    items: [],
  };
  return [...sections, section];
}

export function removeSection(sections: readonly MapSection[], id: string): MapSection[] {
  return sections.filter((s) => s.id !== id);
}

function moved<T>(list: readonly T[], from: number, by: number): T[] {
  const to = from + by;
  if (from < 0 || from >= list.length || to < 0 || to >= list.length) return [...list];
  const out = [...list];
  const [one] = out.splice(from, 1);
  out.splice(to, 0, one as T);
  return out;
}

export const moveSection = (sections: readonly MapSection[], index: number, by: number) =>
  moved(sections, index, by);

export function patchSection(
  sections: readonly MapSection[],
  id: string,
  patch: Partial<Omit<MapSection, "id" | "items">>,
): MapSection[] {
  return sections.map((s) => (s.id === id ? { ...s, ...patch } : s));
}

function withItems(
  sections: readonly MapSection[],
  id: string,
  change: (items: readonly MapItem[]) => MapItem[],
): MapSection[] {
  return sections.map((s) => (s.id === id ? { ...s, items: change(s.items) } : s));
}

export function addItem(sections: readonly MapSection[], sectionId: string): MapSection[] {
  const all = sections.flatMap((s) => s.items.map((i) => i.id));
  return withItems(sections, sectionId, (items) =>
    items.length >= MAX_ITEMS
      ? [...items]
      : [
          ...items,
          {
            id: freshId(all, "i"),
            label: "",
            value: "",
            sub: "",
            icon: null,
            color: null,
            figure: null,
          },
        ],
  );
}

export const removeItem = (sections: readonly MapSection[], sectionId: string, itemId: string) =>
  withItems(sections, sectionId, (items) => items.filter((i) => i.id !== itemId));

export const moveItem = (
  sections: readonly MapSection[],
  sectionId: string,
  index: number,
  by: number,
) => withItems(sections, sectionId, (items) => moved(items, index, by));

export const patchItem = (
  sections: readonly MapSection[],
  sectionId: string,
  itemId: string,
  patch: Partial<Omit<MapItem, "id">>,
) =>
  withItems(sections, sectionId, (items) =>
    items.map((i) => (i.id === itemId ? { ...i, ...patch } : i)),
  );
