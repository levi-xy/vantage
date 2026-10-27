import { useState } from "react";
import type { ChangeEvent, FormEvent, InputHTMLAttributes } from "react";
import { ApiError } from "../api";
import { useSession } from "../app/session";
import { AuthScreen } from "../components/AuthScreen";
import {
  alertError,
  buttonPrimary,
  fieldError,
  fieldHint,
  input,
  label,
} from "../components/styles";

// Same rules as backend/app/api/auth.py (SetupRequest); the backend checks them again.
const USERNAME_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;
const MIN_PASSWORD_LENGTH = 12;
const MAX_PASSWORD_LENGTH = 1024;

interface Fields {
  username: string;
  password: string;
  confirm: string;
}

type FieldErrors = Partial<Record<keyof Fields, string>>;

function validateSetup({ username, password, confirm }: Fields): FieldErrors {
  const errors: FieldErrors = {};
  if (!USERNAME_PATTERN.test(username)) {
    errors.username =
      "1 to 64 characters: letters, digits, dot, dash or underscore, starting with a letter or digit.";
  }
  if (password.length < MIN_PASSWORD_LENGTH) {
    errors.password = `At least ${MIN_PASSWORD_LENGTH} characters.`;
  } else if (password.length > MAX_PASSWORD_LENGTH) {
    errors.password = `At most ${MAX_PASSWORD_LENGTH} characters.`;
  }
  if (confirm !== password) errors.confirm = "The passwords don't match.";
  return errors;
}

function setupError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 0) return "Could not reach the Vantage backend.";
    // Validation messages describe the rules, never the submitted values.
    if (error.status === 422) return error.message;
  }
  return "Setup failed. Try again.";
}

/** First-run wizard: create the admin account. Only reachable while no user exists. */
export function SetupPage() {
  const { setup } = useSession();
  const [fields, setFields] = useState<Fields>({ username: "", password: "", confirm: "" });
  const [errors, setErrors] = useState<FieldErrors>({});
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const update = (name: keyof Fields) => (e: ChangeEvent<HTMLInputElement>) =>
    setFields((f) => ({ ...f, [name]: e.target.value }));

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy) return;
    const found = validateSetup(fields);
    setErrors(found);
    setError(null);
    if (Object.keys(found).length) return;
    setBusy(true);
    try {
      // On success the backend logs the new admin in and the gate opens the dashboard.
      await setup({ username: fields.username, password: fields.password });
    } catch (err) {
      setError(setupError(err));
      setBusy(false);
    }
  }

  const field = (
    name: keyof Fields,
    text: string,
    props: InputHTMLAttributes<HTMLInputElement>,
    hint?: string,
  ) => {
    const id = `setup-${name}`;
    const describedBy = [hint && `${id}-hint`, errors[name] && `${id}-error`]
      .filter(Boolean)
      .join(" ");
    return (
      <div>
        <label htmlFor={id} className={label}>
          {text}
        </label>
        <input
          id={id}
          name={name}
          required
          className={input}
          value={fields[name]}
          onChange={update(name)}
          aria-invalid={errors[name] ? true : undefined}
          aria-describedby={describedBy || undefined}
          {...props}
        />
        {hint && (
          <p id={`${id}-hint`} className={fieldHint}>
            {hint}
          </p>
        )}
        {errors[name] && (
          <p id={`${id}-error`} className={fieldError}>
            {errors[name]}
          </p>
        )}
      </div>
    );
  };

  return (
    <AuthScreen title="Welcome to Vantage">
      <p className="mb-4 text-sm text-slate-600 dark:text-slate-400">
        Create the admin account. You can add read-only viewer accounts later.
      </p>
      <form onSubmit={onSubmit} noValidate className="space-y-4">
        {error && (
          <p role="alert" className={alertError}>
            {error}
          </p>
        )}
        {field("username", "Admin username", {
          autoComplete: "username",
          autoCapitalize: "none",
          spellCheck: false,
          maxLength: 64,
        })}
        {field(
          "password",
          "Password",
          { type: "password", autoComplete: "new-password", maxLength: MAX_PASSWORD_LENGTH },
          `At least ${MIN_PASSWORD_LENGTH} characters.`,
        )}
        {field("confirm", "Confirm password", {
          type: "password",
          autoComplete: "new-password",
          maxLength: MAX_PASSWORD_LENGTH,
        })}
        <button type="submit" disabled={busy} className={`${buttonPrimary} w-full`}>
          {busy ? "Creating account…" : "Create admin account"}
        </button>
      </form>
    </AuthScreen>
  );
}
