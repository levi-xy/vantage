import { useTheme } from "../app/ThemeProvider";
import { nextPreference } from "../app/theme";
import type { ThemePreference } from "../app/theme";

const LABELS: Record<ThemePreference, string> = {
  system: "System",
  light: "Light",
  dark: "Dark",
};

const ICONS: Record<ThemePreference, string> = {
  system: "◐",
  light: "☀",
  dark: "☾",
};

/** Cycles system → light → dark. */
export function ThemeToggle() {
  const { preference, setPreference } = useTheme();
  const next = nextPreference(preference);
  return (
    <button
      type="button"
      onClick={() => setPreference(next)}
      aria-label={`Theme: ${LABELS[preference]}. Switch to ${LABELS[next]}`}
      title={`Theme: ${LABELS[preference]}`}
      className="inline-flex h-9 items-center gap-2 rounded-md border border-slate-300 px-3 text-sm text-slate-700 hover:bg-slate-100 focus-visible:ring-2 focus-visible:ring-sky-500 focus-visible:outline-none dark:border-slate-700 dark:text-slate-200 dark:hover:bg-slate-800"
    >
      <span aria-hidden="true">{ICONS[preference]}</span>
      <span>{LABELS[preference]}</span>
    </button>
  );
}
