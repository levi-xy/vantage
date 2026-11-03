import { colorVar, DEVICE_COLORS } from "../../app/deviceColors";
import type { DeviceColorName } from "../../app/deviceColors";

/**
 * The device colour, as a radio group of swatches: native radios, so Tab reaches the group
 * and the arrow keys move the choice.
 */
export function ColorPicker({
  value,
  onChange,
}: {
  value: string | null;
  onChange: (name: DeviceColorName) => void;
}) {
  return (
    <fieldset>
      <legend className="mb-1 block text-sm font-medium">Colour</legend>
      <div className="flex flex-wrap gap-2">
        {DEVICE_COLORS.map((c) => (
          <label key={c.name} title={c.label} className="cursor-pointer">
            <input
              type="radio"
              name="device-color"
              value={c.name}
              checked={value === c.name}
              onChange={() => onChange(c.name)}
              className="peer sr-only"
            />
            <span className="sr-only">{c.label}</span>
            <span
              aria-hidden="true"
              style={{ backgroundColor: colorVar(c.name) }}
              className="block h-7 w-7 rounded-full ring-offset-2 ring-offset-panel peer-checked:ring-2 peer-checked:ring-ink peer-focus-visible:outline-2 peer-focus-visible:outline-offset-4 peer-focus-visible:outline-in"
            />
          </label>
        ))}
      </div>
      <p className="mt-1 text-xs text-slate-600 dark:text-slate-400">
        Every card of this device shows its colour.
      </p>
    </fieldset>
  );
}
