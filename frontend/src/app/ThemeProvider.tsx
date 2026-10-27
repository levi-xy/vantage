import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import {
  applyTheme,
  readPreference,
  systemTheme,
  watchSystemTheme,
  writePreference,
} from "./theme";
import type { Theme, ThemePreference } from "./theme";

interface ThemeContextValue {
  /** What the user chose. */
  preference: ThemePreference;
  /** What is shown: the preference, or the OS theme when the preference is "system". */
  theme: Theme;
  setPreference: (preference: ThemePreference) => void;
}

const ThemeContext = createContext<ThemeContextValue | null>(null);

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [preference, setPreferenceState] = useState<ThemePreference>(readPreference);
  const [system, setSystem] = useState<Theme>(systemTheme);
  const theme = preference === "system" ? system : preference;

  useEffect(() => watchSystemTheme(() => setSystem(systemTheme())), []);
  useEffect(() => applyTheme(theme), [theme]);

  const setPreference = useCallback((next: ThemePreference) => {
    writePreference(next);
    setPreferenceState(next);
  }, []);

  const value = useMemo(
    () => ({ preference, theme, setPreference }),
    [preference, theme, setPreference],
  );
  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}

// eslint-disable-next-line react-refresh/only-export-components
export function useTheme(): ThemeContextValue {
  const value = useContext(ThemeContext);
  if (!value) throw new Error("useTheme must be used inside <ThemeProvider>");
  return value;
}
