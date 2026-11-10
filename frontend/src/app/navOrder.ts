import { useCallback, useState } from "react";

export const NAV_ORDER_KEY = "vantage.navOrder";

function readSaved(): string[] {
  try {
    const raw = window.localStorage.getItem(NAV_ORDER_KEY);
    if (raw === null) return [];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed) || parsed.length > 50) return [];
    return parsed.filter((id): id is string => typeof id === "string" && id.length <= 64);
  } catch {
    return [];
  }
}

/**
 * Items in the saved order. Ids the list no longer has are dropped; items the saved order
 * doesn't know (new ones) come last, in their default order.
 */
export function applyOrder<T extends { to: string }>(items: readonly T[], saved: string[]): T[] {
  const result: T[] = [];
  for (const id of saved) {
    const item = items.find((i) => i.to === id);
    if (item && !result.includes(item)) result.push(item);
  }
  for (const item of items) if (!result.includes(item)) result.push(item);
  return result;
}

/** Moves `id` to the place of `targetId`; null when nothing would change. */
export function reorder(ids: string[], id: string, targetId: string): string[] | null {
  const from = ids.indexOf(id);
  const to = ids.indexOf(targetId);
  if (from < 0 || to < 0 || from === to) return null;
  const next = ids.slice();
  next.splice(from, 1);
  next.splice(to, 0, id);
  return next;
}

/**
 * The main navigation items in the order this browser prefers (a convenience in
 * localStorage, written only when the order changes).
 */
export function useNavOrder<T extends { to: string }>(
  items: readonly T[],
): { items: T[]; moveTo: (id: string, targetId: string) => void } {
  const [saved, setSaved] = useState<string[]>(readSaved);
  const ordered = applyOrder(items, saved);
  const ids = ordered.map((i) => i.to).join("\n");
  const moveTo = useCallback(
    (id: string, targetId: string) => {
      const next = reorder(ids.split("\n"), id, targetId);
      if (!next) return;
      setSaved(next);
      try {
        window.localStorage.setItem(NAV_ORDER_KEY, JSON.stringify(next));
      } catch {
        // Storage blocked: the order just isn't remembered.
      }
    },
    [ids],
  );
  return { items: ordered, moveTo };
}
