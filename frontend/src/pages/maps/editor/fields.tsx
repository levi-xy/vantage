// Small form pieces shared by the inspector's panels.

import { useState } from "react";
import type { ReactNode } from "react";
import { colorVar, DEVICE_COLORS } from "../../../app/deviceColors";
import { input } from "../../../components/styles";

export const labelText = "mb-1 block text-xs font-medium text-muted";
export const section = "flex flex-col gap-3 border-t border-line pt-3";

export function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="block">
      <span className={labelText}>{label}</span>
      {children}
    </label>
  );
}

export function Check({
  label,
  checked,
  disabled,
  onChange,
}: {
  label: string;
  checked: boolean;
  disabled?: boolean;
  onChange: (v: boolean) => void;
}) {
  return (
    <label className="flex items-center gap-2 text-sm">
      <input
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={(e) => onChange(e.target.checked)}
      />
      {label}
    </label>
  );
}

/** The palette as swatches; `none` is the no-colour choice (device colour, or no colour). */
export function ColorPicker({
  value,
  noneLabel,
  onChange,
}: {
  value: string | null;
  noneLabel: string;
  onChange: (v: string | null) => void;
}) {
  const chip = (active: boolean) =>
    `h-6 w-6 rounded-full border-2 focus-visible:ring-2 focus-visible:ring-in focus-visible:outline-none ${active ? "border-ink" : "border-line"}`;
  return (
    <div role="group" aria-label="Colour" className="flex flex-wrap items-center gap-1.5">
      <button
        type="button"
        aria-label={noneLabel}
        aria-pressed={value === null}
        title={noneLabel}
        className={`${chip(value === null)} bg-chip text-[10px] text-muted`}
        onClick={() => onChange(null)}
      >
        –
      </button>
      {DEVICE_COLORS.map((c) => (
        <button
          key={c.name}
          type="button"
          aria-label={c.label}
          aria-pressed={value === c.name}
          title={c.label}
          className={chip(value === c.name)}
          style={{ background: colorVar(c.name) }}
          onClick={() => onChange(c.name)}
        />
      ))}
    </div>
  );
}

/** A text box that commits on blur or Enter; `accept` can refuse a value (it snaps back). */
export function CommitText({
  label,
  value,
  maxLength,
  accept = (v) => v !== "",
  onCommit,
}: {
  label: string;
  value: string;
  maxLength: number;
  accept?: (v: string) => boolean;
  onCommit: (v: string) => void;
}) {
  const [draft, setDraft] = useState<string | null>(null);
  const commit = () => {
    if (draft !== null && draft !== value && accept(draft)) onCommit(draft);
    setDraft(null);
  };
  return (
    <input
      className={input}
      aria-label={label}
      value={draft ?? value}
      maxLength={maxLength}
      onChange={(e) => setDraft(e.target.value)}
      onBlur={commit}
      onKeyDown={(e) => e.key === "Enter" && commit()}
    />
  );
}
