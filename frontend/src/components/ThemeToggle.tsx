import type { ReactNode } from "react";
import { useTheme } from "../app/ThemeProvider";
import { nextPreference } from "../app/theme";
import type { ThemePreference } from "../app/theme";

const LABELS: Record<ThemePreference, string> = {
  system: "System",
  light: "Light",
  dark: "Dark",
};

const ICONS: Record<ThemePreference, ReactNode> = {
  // Half-filled circle: follows the OS.
  system: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M12 3a9 9 0 0 1 0 18z" fill="currentColor" />
    </>
  ),
  light: (
    <>
      <circle cx="12" cy="12" r="4" />
      <path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" />
    </>
  ),
  dark: <path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z" />,
};

/** Cycles system → light → dark. An icon; the name says the current and next theme. */
export function ThemeToggle() {
  const { preference, setPreference } = useTheme();
  const next = nextPreference(preference);
  return (
    <button
      type="button"
      onClick={() => setPreference(next)}
      aria-label={`Theme: ${LABELS[preference]}. Switch to ${LABELS[next]}`}
      title={`Theme: ${LABELS[preference]}`}
      className="inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border border-line text-muted hover:bg-chip hover:text-ink focus-visible:ring-2 focus-visible:ring-in focus-visible:outline-none"
    >
      <svg
        width="16"
        height="16"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        aria-hidden="true"
      >
        {ICONS[preference]}
      </svg>
    </button>
  );
}
