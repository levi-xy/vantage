import { createContext, useCallback, useContext, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { formatBitsPerSecond, formatBytesPerSecond } from "../cards/metrics/format";

/** How traffic rates are shown: bits (Mbit/s, as link speeds are quoted) or bytes (MB/s). */
export type RateUnit = "bits" | "bytes";

const STORAGE_KEY = "vantage.rateUnit";

function readUnit(): RateUnit {
  try {
    return window.localStorage.getItem(STORAGE_KEY) === "bytes" ? "bytes" : "bits";
  } catch {
    return "bits"; // storage blocked: the default
  }
}

function writeUnit(unit: RateUnit) {
  try {
    window.localStorage.setItem(STORAGE_KEY, unit);
  } catch {
    // storage blocked: the choice lasts until the page reloads
  }
}

interface UnitsContextValue {
  unit: RateUnit;
  setUnit: (unit: RateUnit) => void;
  /** Formats a rate given in bits per second, in the chosen unit. */
  formatRate: (bitsPerSecond: number | undefined) => string;
}

const formatterFor = (unit: RateUnit) =>
  unit === "bytes" ? formatBytesPerSecond : formatBitsPerSecond;

// Outside a provider (card tests): bits, and changing it does nothing.
const UnitsContext = createContext<UnitsContextValue>({
  unit: "bits",
  setUnit: () => undefined,
  formatRate: formatBitsPerSecond,
});

/** The rate unit is a per-browser display preference (not a secret, so localStorage is fine). */
export function UnitsProvider({ children }: { children: ReactNode }) {
  const [unit, setUnitState] = useState<RateUnit>(readUnit);
  const setUnit = useCallback((next: RateUnit) => {
    writeUnit(next);
    setUnitState(next);
  }, []);
  const value = useMemo(() => ({ unit, setUnit, formatRate: formatterFor(unit) }), [unit, setUnit]);
  return <UnitsContext.Provider value={value}>{children}</UnitsContext.Provider>;
}

// eslint-disable-next-line react-refresh/only-export-components
export function useUnits(): UnitsContextValue {
  return useContext(UnitsContext);
}
