import { useState } from "react";
import type { ReactNode } from "react";
import type { MapIcon as IconName, MapIconRef } from "../../api";

const PATHS: Record<IconName, ReactNode> = {
  internet: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M3 12h18M12 3c3 3 3 15 0 18M12 3c-3 3-3 15 0 18" />
    </>
  ),
  cloud: <path d="M7 18a4 4 0 0 1-.5-8 5.5 5.5 0 0 1 10.6 1.3A3.4 3.4 0 0 1 17 18z" />,
  router: (
    <>
      <rect x="3" y="13" width="18" height="6" rx="1.5" />
      <path d="M7 16h.01M11 16h.01M8 13V9M16 13V9M5 6a4 4 0 0 1 3-1M19 6a4 4 0 0 0-3-1" />
    </>
  ),
  firewall: (
    <>
      <rect x="3" y="5" width="18" height="14" rx="1.5" />
      <path d="M3 10h18M3 14.5h18M9 5v5M15 10v4.5M9 14.5V19" />
    </>
  ),
  switch: (
    <>
      <rect x="2" y="8" width="20" height="8" rx="1.5" />
      <path d="M6 12h.01M10 12h.01M14 12h.01M18 12h.01" />
    </>
  ),
  server: (
    <>
      <rect x="4" y="4" width="16" height="7" rx="1.5" />
      <rect x="4" y="13" width="16" height="7" rx="1.5" />
      <path d="M8 7.5h.01M8 16.5h.01" />
    </>
  ),
  nas: (
    <>
      <rect x="5" y="3" width="14" height="18" rx="1.5" />
      <path d="M9 7v6M12 7v6M15 7v6M9 17h.01" />
    </>
  ),
  ap: (
    <>
      <circle cx="12" cy="14" r="2" />
      <path d="M8 10a5.7 5.7 0 0 1 8 0M5 7a10 10 0 0 1 14 0M12 16v4" />
    </>
  ),
  pc: (
    <>
      <rect x="3" y="4" width="18" height="12" rx="1.5" />
      <path d="M9 20h6M12 16v4" />
    </>
  ),
  laptop: (
    <>
      <rect x="5" y="5" width="14" height="10" rx="1.5" />
      <path d="M2 19h20" />
    </>
  ),
  phone: (
    <>
      <rect x="7" y="2" width="10" height="20" rx="2" />
      <path d="M11 18h2" />
    </>
  ),
  camera: (
    <>
      <path d="M3 8h4l1.5-2h7L17 8h4v11H3z" />
      <circle cx="12" cy="13" r="3.5" />
    </>
  ),
  printer: (
    <>
      <path d="M7 9V3h10v6M7 17H4v-7h16v7h-3" />
      <rect x="7" y="14" width="10" height="7" />
    </>
  ),
  iot: (
    <>
      <rect x="7" y="7" width="10" height="10" rx="1.5" />
      <path d="M10 3v4M14 3v4M10 17v4M14 17v4M3 10h4M3 14h4M17 10h4M17 14h4" />
    </>
  ),
  generic: <rect x="4" y="4" width="16" height="16" rx="3" />,
};

const DI = /^di:([a-z0-9-]{1,64})$/;

/** Where a library icon is served from: Vantage's own web server, never a CDN. */
const libraryIconUrl = (slug: string) => `/icons/di/${slug}.svg`;

function Glyph({ icon, size }: { icon: IconName; size: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {PATHS[icon] ?? PATHS.generic}
    </svg>
  );
}

/**
 * A box's icon: one of the generic stroke glyphs, or a library icon (`di:<slug>`) shown as an
 * image (never inlined, so an SVG can't run script). A library file that is missing, or an
 * unknown name, shows the generic glyph.
 */
export function MapIcon({ icon, size = 18 }: { icon: MapIconRef; size?: number }) {
  const [failed, setFailed] = useState<string | null>(null);
  const slug = DI.exec(icon)?.[1];
  if (slug && failed !== slug) {
    return (
      <img
        src={libraryIconUrl(slug)}
        width={size}
        height={size}
        alt=""
        loading="lazy"
        onError={() => setFailed(slug)}
      />
    );
  }
  return <Glyph icon={slug ? "generic" : (icon as IconName)} size={size} />;
}
