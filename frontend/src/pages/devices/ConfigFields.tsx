import { fieldError, fieldHint, input, label } from "../../components/styles";
import type { ConfigField, FieldValue, FormValues } from "./schemaForm";

interface Props {
  fields: ConfigField[];
  values: FormValues;
  errors: Record<string, string>;
  onChange: (key: string, value: FieldValue) => void;
  /** Editing a saved instance: secrets show "set" / "not set" and blank means unchanged. */
  editing: boolean;
  secretsSet: Record<string, boolean>;
  clearSecrets: ReadonlySet<string>;
  onClearSecret: (key: string, clear: boolean) => void;
}

function SecretBadge({ set }: { set: boolean }) {
  return set ? (
    <span className="ml-2 rounded bg-emerald-100 px-1.5 py-0.5 text-xs font-medium text-emerald-800 dark:bg-emerald-950 dark:text-emerald-200">
      set
    </span>
  ) : (
    <span className="ml-2 rounded bg-slate-200 px-1.5 py-0.5 text-xs font-medium text-slate-700 dark:bg-slate-800 dark:text-slate-300">
      not set
    </span>
  );
}

/** The inputs generated from an integration's config schema. */
export function ConfigFields(props: Props) {
  const { fields, values, errors, onChange, editing, secretsSet, clearSecrets, onClearSecret } =
    props;

  return (
    <>
      {fields.map((field) => {
        const id = `config-${field.key}`;
        const error = errors[field.key];
        const hints: string[] = [];
        if (field.description) hints.push(field.description);
        if (field.secret && editing) {
          hints.push(secretsSet[field.key] ? "Leave blank to keep the stored value." : "");
        }
        if (field.kind === "list") hints.push("One per line.");
        const hintText = hints.filter(Boolean).join(" ");
        const describedBy =
          [hintText ? `${id}-hint` : "", error ? `${id}-error` : ""].filter(Boolean).join(" ") ||
          undefined;
        const value = values[field.key];
        const text = typeof value === "string" ? value : "";
        const clearing = clearSecrets.has(field.key);

        if (field.kind === "unsupported") {
          return (
            <div key={field.key}>
              <p className={label}>{field.label}</p>
              <p className={fieldHint}>
                This setting can't be edited here yet; its default is used.
              </p>
            </div>
          );
        }

        if (field.kind === "boolean") {
          return (
            <div key={field.key}>
              <label htmlFor={id} className="flex items-center gap-2 text-sm font-medium">
                <input
                  id={id}
                  type="checkbox"
                  className="h-4 w-4"
                  checked={value === true}
                  aria-describedby={describedBy}
                  onChange={(e) => onChange(field.key, e.target.checked)}
                />
                {field.label}
              </label>
              {hintText && (
                <p id={`${id}-hint`} className={fieldHint}>
                  {hintText}
                </p>
              )}
            </div>
          );
        }

        const common = {
          id,
          name: field.key,
          className: input,
          "aria-invalid": error ? true : undefined,
          "aria-describedby": describedBy,
          "aria-required": field.required && !(field.secret && editing && secretsSet[field.key]),
        } as const;

        let control;
        if (field.kind === "enum") {
          control = (
            <select {...common} value={text} onChange={(e) => onChange(field.key, e.target.value)}>
              {!field.required && <option value="">(default)</option>}
              {field.required && text === "" && <option value="">Choose…</option>}
              {field.options?.map((o) => (
                <option key={String(o)} value={String(o)}>
                  {String(o)}
                </option>
              ))}
            </select>
          );
        } else if (field.kind === "list") {
          control = (
            <textarea
              {...common}
              rows={3}
              value={text}
              spellCheck={false}
              onChange={(e) => onChange(field.key, e.target.value)}
            />
          );
        } else if (field.secret) {
          control = (
            <input
              {...common}
              type="password"
              // Not the user's Vantage login: keep the browser from filling it in.
              autoComplete="new-password"
              spellCheck={false}
              value={text}
              disabled={clearing}
              placeholder={editing && secretsSet[field.key] ? "Unchanged" : undefined}
              onChange={(e) => onChange(field.key, e.target.value)}
            />
          );
        } else {
          const numeric = field.kind === "integer" || field.kind === "number";
          control = (
            <input
              {...common}
              type="text"
              inputMode={field.kind === "integer" ? "numeric" : numeric ? "decimal" : undefined}
              autoComplete="off"
              autoCapitalize="none"
              spellCheck={false}
              placeholder={field.placeholder}
              value={text}
              onChange={(e) => onChange(field.key, e.target.value)}
            />
          );
        }

        return (
          <div key={field.key}>
            <label htmlFor={id} className={label}>
              {field.label}
              {!field.required && !field.secret && (
                <span className="font-normal text-slate-500 dark:text-slate-400"> (optional)</span>
              )}
              {field.secret && editing && <SecretBadge set={secretsSet[field.key] === true} />}
            </label>
            {control}
            {field.secret && editing && field.nullable && secretsSet[field.key] && (
              <label className="mt-1 flex items-center gap-2 text-xs">
                <input
                  type="checkbox"
                  checked={clearing}
                  onChange={(e) => onClearSecret(field.key, e.target.checked)}
                />
                Remove the stored value
              </label>
            )}
            {hintText && (
              <p id={`${id}-hint`} className={fieldHint}>
                {hintText}
              </p>
            )}
            {error && (
              <p id={`${id}-error`} className={fieldError}>
                {error}
              </p>
            )}
          </div>
        );
      })}
    </>
  );
}
