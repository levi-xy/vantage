// The search index of the shipped icon library (public/icons/di/index.json, written by
// scripts/fetch-icons.mjs and served by Vantage's own web server). Missing = empty library.

export interface LibraryIcon {
  slug: string;
  name: string;
  aliases: string[];
}

let cached: Promise<LibraryIcon[]> | null = null;

/** The library's index, read once. Any failure (no icons installed, bad answer) gives []. */
export function loadIconIndex(): Promise<LibraryIcon[]> {
  cached ??= fetch("/icons/di/index.json")
    .then((r) => (r.ok ? r.json() : []))
    .then((data: unknown) =>
      Array.isArray(data)
        ? data.filter(
            (e): e is LibraryIcon =>
              typeof e === "object" &&
              e !== null &&
              typeof (e as LibraryIcon).slug === "string" &&
              /^[a-z0-9-]{1,64}$/.test((e as LibraryIcon).slug),
          )
        : [],
    )
    .catch(() => []);
  return cached;
}

/** Forget the cached index (tests). */
export function resetIconIndex(): void {
  cached = null;
}

/** Icons whose slug, name or an alias contains every word of the query; the first `limit`. */
export function searchIcons(
  index: readonly LibraryIcon[],
  query: string,
  limit = 60,
): LibraryIcon[] {
  const words = query.toLowerCase().split(/\s+/).filter(Boolean);
  if (words.length === 0) return index.slice(0, limit);
  const out: LibraryIcon[] = [];
  for (const icon of index) {
    const hay = [icon.slug, icon.name, ...icon.aliases].join(" ").toLowerCase();
    if (words.every((w) => hay.includes(w))) out.push(icon);
    if (out.length >= limit) break;
  }
  return out;
}
