// Light/dark mode. Follows the OS by default; the user can pin light or dark.
// Only this display preference goes in localStorage: never tokens or session data.

export type ThemePreference = "system" | "light" | "dark";
export type Theme = "light" | "dark";

export const THEME_STORAGE_KEY = "vantage.theme";
const DARK_QUERY = "(prefers-color-scheme: dark)";

export function readPreference(): ThemePreference {
  try {
    const value = window.localStorage.getItem(THEME_STORAGE_KEY);
    if (value === "light" || value === "dark") return value;
  } catch {
    // Storage blocked (private mode, policy): fall back to the OS preference.
  }
  return "system";
}

export function writePreference(preference: ThemePreference): void {
  try {
    if (preference === "system") window.localStorage.removeItem(THEME_STORAGE_KEY);
    else window.localStorage.setItem(THEME_STORAGE_KEY, preference);
  } catch {
    // Not persisted; the choice still applies for this page load.
  }
}

function darkQuery(): MediaQueryList | null {
  return typeof window.matchMedia === "function" ? window.matchMedia(DARK_QUERY) : null;
}

export function systemTheme(): Theme {
  return darkQuery()?.matches ? "dark" : "light";
}

/** Call `onChange` when the OS switches between light and dark. Returns an unsubscribe. */
export function watchSystemTheme(onChange: () => void): () => void {
  const query = darkQuery();
  if (!query) return () => {};
  query.addEventListener("change", onChange);
  return () => query.removeEventListener("change", onChange);
}

export function resolveTheme(preference: ThemePreference): Theme {
  return preference === "system" ? systemTheme() : preference;
}

/** Tailwind's `dark:` variant keys off the `dark` class on <html>. */
export function applyTheme(theme: Theme): void {
  const root = document.documentElement;
  root.classList.toggle("dark", theme === "dark");
  root.style.colorScheme = theme;
}

/** The next preference when the toggle is pressed: system → light → dark → system. */
export function nextPreference(preference: ThemePreference): ThemePreference {
  return preference === "system" ? "light" : preference === "light" ? "dark" : "system";
}
