// "My account" (M3.5): every user, any role, can change their own password here.
//
// The password inputs start empty and are cleared after every submit, whatever the
// outcome. The passwords are sent only in the request body, and no response returns them.

import { useMutation } from "@tanstack/react-query";
import { useState } from "react";
import type { FormEvent } from "react";
import { ApiError } from "../api";
import { useApiClient, useUser } from "../app/session";
import { PageHeader } from "../components/PageHeader";
import {
  alertError,
  alertInfo,
  buttonPrimary,
  fieldHint,
  input,
  label,
} from "../components/styles";
import { MAX_PASSWORD_LENGTH, newPasswordProblem, PASSWORD_HINT } from "./users/passwordRules";
import { useUnits } from "../app/units";
import type { RateUnit } from "../app/units";

const ROLE_LABELS = { admin: "Admin", viewer: "Viewer" } as const;

function changeError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 400) return "The current password is wrong.";
    if (error.status === 429) {
      return "Too many wrong passwords. Wait a few minutes and try again.";
    }
    if (error.status === 422) return `The new password must be ${PASSWORD_HINT.toLowerCase()}`;
    // 409: an admin reset the password meanwhile. The backend's message says so.
    return error.message;
  }
  return "Could not change the password. Try again.";
}

function ChangePasswordForm() {
  const { username } = useUser();
  const api = useApiClient();
  const [current, setCurrent] = useState("");
  const [password, setPassword] = useState("");
  const [repeated, setRepeated] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(false);

  const change = useMutation({
    mutationFn: (body: { current_password: string; new_password: string }) =>
      api.changeMyPassword(body),
    onSuccess: () => setDone(true),
    onError: (e) => setError(changeError(e)),
    onSettled: () => {
      setCurrent("");
      setPassword("");
      setRepeated("");
    },
  });

  function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (change.isPending) return;
    setDone(false);
    if (!current) {
      setError("Enter your current password.");
      return;
    }
    const problem = newPasswordProblem(password, repeated);
    if (problem) {
      setError(problem);
      return;
    }
    setError(null);
    change.mutate({ current_password: current, new_password: password });
  }

  return (
    <section
      aria-labelledby="change-password-title"
      className="max-w-lg rounded-lg border border-slate-200 bg-white p-4 dark:border-slate-800 dark:bg-slate-900"
    >
      <h2 id="change-password-title" className="mb-1 text-base font-semibold">
        Change my password
      </h2>
      <p className="mb-4 text-sm text-slate-600 dark:text-slate-400">
        Your other sessions (other browsers and devices) are logged out. This one stays.
      </p>
      <form onSubmit={onSubmit} noValidate className="space-y-4">
        {/* Lets password managers tie the new password to the right account. */}
        <input
          type="text"
          name="username"
          autoComplete="username"
          value={username}
          readOnly
          hidden
        />
        <div>
          <label htmlFor="current-password" className={label}>
            Current password
          </label>
          <input
            id="current-password"
            type="password"
            autoComplete="current-password"
            maxLength={MAX_PASSWORD_LENGTH}
            className={input}
            value={current}
            onChange={(e) => setCurrent(e.target.value)}
          />
        </div>
        <div>
          <label htmlFor="my-new-password" className={label}>
            New password
          </label>
          <input
            id="my-new-password"
            type="password"
            autoComplete="new-password"
            maxLength={MAX_PASSWORD_LENGTH}
            className={input}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            aria-describedby="my-new-password-hint"
          />
          <p id="my-new-password-hint" className={fieldHint}>
            {PASSWORD_HINT}
          </p>
        </div>
        <div>
          <label htmlFor="my-new-password-repeat" className={label}>
            Repeat the new password
          </label>
          <input
            id="my-new-password-repeat"
            type="password"
            autoComplete="new-password"
            maxLength={MAX_PASSWORD_LENGTH}
            className={input}
            value={repeated}
            onChange={(e) => setRepeated(e.target.value)}
          />
        </div>
        {error && (
          <p role="alert" className={alertError}>
            {error}
          </p>
        )}
        {done && (
          <p role="status" className={alertInfo}>
            Password changed. Your other sessions were logged out.
          </p>
        )}
        <button type="submit" disabled={change.isPending} className={buttonPrimary}>
          {change.isPending ? "Saving…" : "Change password"}
        </button>
      </form>
    </section>
  );
}

function DisplaySettings() {
  const { unit, setUnit } = useUnits();
  const options: { value: RateUnit; label: string }[] = [
    { value: "bits", label: "Bits per second (Mbit/s), as link speeds are quoted" },
    { value: "bytes", label: "Bytes per second (MB/s), as downloads are shown" },
  ];
  return (
    <section
      aria-labelledby="display-title"
      className="mb-4 max-w-lg rounded-lg border border-slate-200 bg-white p-4 dark:border-slate-800 dark:bg-slate-900"
    >
      <h2 id="display-title" className="mb-1 text-base font-semibold">
        Display
      </h2>
      <fieldset>
        <legend className="mb-2 text-sm text-slate-600 dark:text-slate-400">
          Traffic rates (this browser only)
        </legend>
        {options.map((o) => (
          <label key={o.value} className="flex items-center gap-2 py-0.5 text-sm">
            <input
              type="radio"
              name="rate-unit"
              value={o.value}
              checked={unit === o.value}
              onChange={() => setUnit(o.value)}
            />
            {o.label}
          </label>
        ))}
      </fieldset>
    </section>
  );
}

export function AccountPage() {
  const user = useUser();
  return (
    <>
      <PageHeader title="My account" />
      <p className="mb-4 text-sm">
        Logged in as <span className="font-medium">{user.username}</span>{" "}
        <span className="text-slate-500 dark:text-slate-400">({ROLE_LABELS[user.role]})</span>
      </p>
      <DisplaySettings />
      <ChangePasswordForm />
    </>
  );
}
