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

// The one-time code from setup.sh (backend/app/core/setup_code.py); the backend checks it.
const MAX_SETUP_CODE_LENGTH = 64;
const SETUP_CODE_HINT =
  "Printed by setup.sh. To get a new one: docker compose exec backend python -m app.cli setup-code";

interface Fields {
  code: string;
  username: string;
  password: string;
  confirm: string;
}

type FieldErrors = Partial<Record<keyof Fields, string>>;

function validateSetup({ code, username, password, confirm }: Fields): FieldErrors {
  const errors: FieldErrors = {};
  if (!code.trim()) errors.code = "Enter the setup code.";
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
    if (error.status === 403 && error.reason === "setup_code_invalid") {
      return "That setup code is wrong or was already used. Check it, or get a new one with: docker compose exec backend python -m app.cli setup-code";
    }
    if (error.status === 429) return "Too many failed attempts. Wait a few minutes and try again.";
  }
  return "Setup failed. Try again.";
}

/** First-run wizard: create the admin account. Only reachable while no user exists. */
export function SetupPage() {
  const { setup } = useSession();
  const [fields, setFields] = useState<Fields>({
    code: "",
    username: "",
    password: "",
    confirm: "",
  });
  const [errors, setErrors] = useState<FieldErrors>({});
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // Client-side only: the backend doesn't need it and never receives it.
  const [keyBackedUp, setKeyBackedUp] = useState(false);

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
      await setup({
        username: fields.username,
        password: fields.password,
        setup_code: fields.code,
      });
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
        {field(
          "code",
          "Setup code",
          {
            autoComplete: "one-time-code",
            autoCapitalize: "characters",
            spellCheck: false,
            maxLength: MAX_SETUP_CODE_LENGTH,
          },
          SETUP_CODE_HINT,
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
        <section
          aria-labelledby="setup-key-heading"
          className="rounded-md border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900 dark:border-amber-700 dark:bg-amber-950/40 dark:text-amber-100"
        >
          <h2 id="setup-key-heading" className="font-semibold">
            Back up your master key
          </h2>
          <p className="mt-1">
            The master key encrypts every device credential and channel token Vantage stores. If you
            lose it, you have to enter all of them again; a backup of the database is useless
            without it.
          </p>
          <p className="mt-1">
            It is the file <code>secrets/master.key</code> that setup.sh created (on the standalone
            compose install: the <code>master-key</code> volume). Keep a copy somewhere safe,
            outside this server. docs/OPERATIONS.md shows how.
          </p>
          <label className="mt-2 flex items-start gap-2 font-medium">
            <input
              type="checkbox"
              checked={keyBackedUp}
              onChange={(e) => setKeyBackedUp(e.target.checked)}
              className="mt-0.5"
            />
            I have backed up my master key
          </label>
        </section>
        <button type="submit" disabled={busy || !keyBackedUp} className={`${buttonPrimary} w-full`}>
          {busy ? "Creating account…" : "Create admin account"}
        </button>
      </form>
    </AuthScreen>
  );
}
