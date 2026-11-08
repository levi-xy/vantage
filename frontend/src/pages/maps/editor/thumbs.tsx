// Small drawings for the shape and panel-look pickers. They use the theme tokens, so they
// follow light and dark mode.

import type { MapBox, MapPanel } from "../../../api";

const LINE = "var(--vt-muted)";
const FILL = "var(--vt-chip)";

function Thumb({ children }: { children: React.ReactNode }) {
  return (
    <svg aria-hidden="true" viewBox="0 0 64 34" width="64" height="34" fill="none">
      {children}
    </svg>
  );
}

export function ShapeThumb({ shape }: { shape: NonNullable<MapBox["shape"]> }) {
  switch (shape) {
    case "cloud":
      return (
        <Thumb>
          <path
            d="M18 28 H48 A8 8 0 0 0 50 12 A11 11 0 0 0 30 8 A9 9 0 0 0 18 28 Z"
            fill={FILL}
            stroke={LINE}
            strokeWidth="1.2"
          />
        </Thumb>
      );
    case "device":
      return (
        <Thumb>
          <rect x="3" y="9" width="58" height="16" rx="2" fill={FILL} stroke={LINE} />
          <rect x="1" y="12" width="3" height="10" fill={LINE} />
          <rect x="60" y="12" width="3" height="10" fill={LINE} />
          <rect x="14" y="14" width="36" height="6" rx="1" stroke={LINE} />
        </Thumb>
      );
    case "nas":
      return (
        <Thumb>
          <rect x="6" y="4" width="52" height="26" rx="2" fill={FILL} stroke={LINE} />
          {[12, 22, 32, 42].map((x) => (
            <rect key={x} x={x} y="8" width="8" height="18" rx="1" stroke={LINE} />
          ))}
        </Thumb>
      );
    case "ap":
      return (
        <Thumb>
          <path
            d="M10 28 V20 A22 14 0 0 1 54 20 V28 Z"
            fill={FILL}
            stroke={LINE}
            strokeWidth="1.2"
          />
        </Thumb>
      );
    default:
      return (
        <Thumb>
          <rect x="8" y="8" width="48" height="18" rx="4" fill={FILL} stroke={LINE} />
        </Thumb>
      );
  }
}

export function LookThumb({ look }: { look: MapPanel["look"] }) {
  return (
    <Thumb>
      {look === "rack" && (
        <>
          <rect x="2" y="9" width="60" height="16" rx="1.5" fill={FILL} stroke={LINE} />
          <rect x="2" y="9" width="5" height="16" fill={LINE} opacity="0.4" />
          <rect x="57" y="9" width="5" height="16" fill={LINE} opacity="0.4" />
        </>
      )}
      {look === "desktop" && (
        <>
          <rect x="6" y="9" width="52" height="15" rx="2" fill={FILL} stroke={LINE} />
          <rect x="12" y="25" width="9" height="3" fill={LINE} />
          <rect x="43" y="25" width="9" height="3" fill={LINE} />
        </>
      )}
      {look === "flat" && (
        <rect x="6" y="11" width="52" height="12" rx="2" fill={FILL} stroke={LINE} />
      )}
      {[0, 1, 2, 3, 4, 5].map((i) => (
        <rect key={i} x={19 + i * 5} y="14" width="3" height="5" fill={LINE} />
      ))}
    </Thumb>
  );
}
