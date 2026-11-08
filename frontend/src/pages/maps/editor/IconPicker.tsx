// Chooses a box's icon: the shipped library (searchable) or one of the generic glyphs.

import { useEffect, useState } from "react";
import type { MapIconRef } from "../../../api";
import { input } from "../../../components/styles";
import { MapIcon } from "../MapIcon";
import { loadIconIndex, searchIcons } from "./iconIndex";
import type { LibraryIcon } from "./iconIndex";
import { ICONS } from "./model";

const tile = (on: boolean) =>
  `flex h-10 w-10 items-center justify-center rounded-md border ${
    on ? "border-accent bg-chip" : "border-line"
  } hover:bg-chip`;

export function IconPicker({
  value,
  onChange,
}: {
  value: MapIconRef;
  onChange: (icon: MapIconRef) => void;
}) {
  const [tab, setTab] = useState<"library" | "generic">(
    value.startsWith("di:") ? "library" : "generic",
  );
  const [query, setQuery] = useState("");
  const [index, setIndex] = useState<LibraryIcon[] | null>(null);

  useEffect(() => {
    let live = true;
    void loadIconIndex().then((i) => live && setIndex(i));
    return () => {
      live = false;
    };
  }, []);

  const found = index ? searchIcons(index, query) : [];
  return (
    <div className="flex flex-col gap-2" role="group" aria-label="Icon">
      <div className="flex gap-1.5">
        {(["generic", "library"] as const).map((t) => (
          <button
            key={t}
            type="button"
            aria-pressed={tab === t}
            className={`rounded-md px-2 py-1 text-xs ${tab === t ? "bg-chip font-semibold" : "text-muted"}`}
            onClick={() => setTab(t)}
          >
            {t === "generic" ? "Generic" : "Library"}
          </button>
        ))}
      </div>
      {tab === "generic" ? (
        <div className="flex flex-wrap gap-1.5">
          {ICONS.map((i) => (
            <button
              key={i}
              type="button"
              title={i}
              aria-label={`Generic icon ${i}`}
              aria-pressed={value === i}
              className={tile(value === i)}
              onClick={() => onChange(i)}
            >
              <MapIcon icon={i} />
            </button>
          ))}
        </div>
      ) : (
        <>
          <input
            className={input}
            type="search"
            aria-label="Search icons"
            placeholder="Search icons"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
          {index !== null && index.length === 0 && (
            <p className="text-xs text-muted">The icon library isn&apos;t installed.</p>
          )}
          <div className="flex max-h-48 flex-wrap gap-1.5 overflow-y-auto">
            {found.map((i) => (
              <button
                key={i.slug}
                type="button"
                title={i.name}
                aria-label={`Library icon ${i.name}`}
                aria-pressed={value === `di:${i.slug}`}
                className={tile(value === `di:${i.slug}`)}
                onClick={() => onChange(`di:${i.slug}`)}
              >
                <MapIcon icon={`di:${i.slug}`} size={28} />
              </button>
            ))}
          </div>
          {index !== null && index.length > 0 && found.length === 0 && (
            <p className="text-xs text-muted">No icon matches.</p>
          )}
        </>
      )}
    </div>
  );
}
