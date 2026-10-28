// Admin-only user management (M3.5). The route is guarded by <RequireRole role="admin">,
// and the backend enforces the role on every /users request itself.
//
// Passwords are typed into password inputs that start empty, are cleared after every
// submit, and are sent only in the request body. No response carries one.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import type { FormEvent } from "react";
import { Link, useNavigate } from "react-router";
import { ApiError } from "../api";
import type { Role, UserInfo } from "../api";
import { useApiClient, useSession, useUser } from "../app/session";
import { PageHeader } from "../components/PageHeader";
import {
  alertError,
  alertInfo,
  buttonPrimary,
  buttonSecondary,
  fieldHint,
  input,
  label,
} from "../components/styles";
import {
  MAX_PASSWORD_LENGTH,
  newPasswordProblem,
  PASSWORD_HINT,
  USERNAME_HINT,
  USERNAME_PATTERN,
} from "./users/passwordRules";

const USERS_KEY = ["users"] as const;

const ROLE_LABELS: Record<Role, string> = { admin: "Admin", viewer: "Viewer" };

const badge = "inline-flex w-fit rounded px-2 py-0.5 text-xs font-medium";
const ROLE_STYLES: Record<Role, string> = {
  admin: "bg-sky-100 text-sky-800 dark:bg-sky-950 dark:text-sky-200",
  viewer: "bg-slate-200 text-slate-700 dark:bg-slate-800 dark:text-slate-300",
};

const buttonDanger = `${buttonSecondary} border-red-400 text-red-700 dark:border-red-700 dark:text-red-300`;

/** A readable line for a failed request. API errors never carry secrets (backend rule). */
function errorText(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 403) return "Only admins can manage users.";
    // 409: the last-admin rule, your own account, or a taken username. The backend's
    // message says which.
    if (error.status === 409) return `Not done: ${error.message}.`;
    if (error.status === 422) return "Check the fields and try again.";
    return error.message;
  }
  return "Something went wrong. Try again.";
}

function formatTime(iso: string | null): string {
  if (!iso) return "Never";
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString();
}

function useUsers() {
  const api = useApiClient();
  return useQuery({ queryKey: USERS_KEY, queryFn: () => api.users(), staleTime: 0 });
}

/** A new password, typed twice. */
function NewPasswordFields({
  idPrefix,
  password,
  repeated,
  onPassword,
  onRepeated,
}: {
  idPrefix: string;
  password: string;
  repeated: string;
  onPassword: (value: string) => void;
  onRepeated: (value: string) => void;
}) {
  return (
    <>
      <div>
        <label htmlFor={`${idPrefix}-password`} className={label}>
          New password
        </label>
        <input
          id={`${idPrefix}-password`}
          type="password"
          autoComplete="new-password"
          maxLength={MAX_PASSWORD_LENGTH}
          className={input}
          value={password}
          onChange={(e) => onPassword(e.target.value)}
          aria-describedby={`${idPrefix}-password-hint`}
        />
        <p id={`${idPrefix}-password-hint`} className={fieldHint}>
          {PASSWORD_HINT}
        </p>
      </div>
      <div>
        <label htmlFor={`${idPrefix}-repeat`} className={label}>
          Repeat the new password
        </label>
        <input
          id={`${idPrefix}-repeat`}
          type="password"
          autoComplete="new-password"
          maxLength={MAX_PASSWORD_LENGTH}
          className={input}
          value={repeated}
          onChange={(e) => onRepeated(e.target.value)}
        />
      </div>
    </>
  );
}

function CreateUserForm() {
  const api = useApiClient();
  const queryClient = useQueryClient();
  const [username, setUsername] = useState("");
  const [role, setRole] = useState<Role>("viewer");
  const [password, setPassword] = useState("");
  const [repeated, setRepeated] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [created, setCreated] = useState<string | null>(null);

  const create = useMutation({
    mutationFn: (body: { username: string; password: string; role: Role }) => api.createUser(body),
    onSuccess: (user) => {
      setCreated(user.username);
      setUsername("");
      setRole("viewer");
      void queryClient.invalidateQueries({ queryKey: USERS_KEY });
    },
    onError: (e) => setError(errorText(e)),
    // Never kept around in the form, whatever happened.
    onSettled: () => {
      setPassword("");
      setRepeated("");
    },
  });

  function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (create.isPending) return;
    setCreated(null);
    if (!USERNAME_PATTERN.test(username)) {
      setError(`Invalid username. ${USERNAME_HINT}`);
      return;
    }
    const problem = newPasswordProblem(password, repeated);
    if (problem) {
      setError(problem);
      return;
    }
    setError(null);
    create.mutate({ username, password, role });
  }

  return (
    <section
      aria-labelledby="create-user-title"
      className="rounded-lg border border-slate-200 bg-white p-4 dark:border-slate-800 dark:bg-slate-900"
    >
      <h2 id="create-user-title" className="mb-3 text-base font-semibold">
        Add a user
      </h2>
      <form onSubmit={onSubmit} noValidate className="grid gap-4 md:grid-cols-2">
        <div>
          <label htmlFor="new-user-name" className={label}>
            Username
          </label>
          <input
            id="new-user-name"
            autoComplete="off"
            autoCapitalize="none"
            spellCheck={false}
            maxLength={64}
            className={input}
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            aria-describedby="new-user-name-hint"
          />
          <p id="new-user-name-hint" className={fieldHint}>
            {USERNAME_HINT}
          </p>
        </div>
        <div>
          <label htmlFor="new-user-role" className={label}>
            Role
          </label>
          <select
            id="new-user-role"
            className={input}
            value={role}
            onChange={(e) => setRole(e.target.value as Role)}
          >
            <option value="viewer">Viewer (read only)</option>
            <option value="admin">Admin</option>
          </select>
        </div>
        <NewPasswordFields
          idPrefix="new-user"
          password={password}
          repeated={repeated}
          onPassword={setPassword}
          onRepeated={setRepeated}
        />
        <div className="flex flex-col gap-3 md:col-span-2">
          {error && (
            <p role="alert" className={alertError}>
              {error}
            </p>
          )}
          {created && (
            <p role="status" className={alertInfo}>
              Added {created}.
            </p>
          )}
          <button type="submit" disabled={create.isPending} className={`${buttonPrimary} w-fit`}>
            {create.isPending ? "Adding…" : "Add user"}
          </button>
        </div>
      </form>
    </section>
  );
}

function ResetPasswordForm({ user, onDone }: { user: UserInfo; onDone: (msg: string) => void }) {
  const api = useApiClient();
  const [password, setPassword] = useState("");
  const [repeated, setRepeated] = useState("");
  const [error, setError] = useState<string | null>(null);

  const reset = useMutation({
    mutationFn: (newPassword: string) => api.resetUserPassword(user.id, newPassword),
    onSuccess: () =>
      onDone(`New password set for ${user.username}. Their sessions were logged out.`),
    onError: (e) => setError(errorText(e)),
    onSettled: () => {
      setPassword("");
      setRepeated("");
    },
  });

  function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (reset.isPending) return;
    const problem = newPasswordProblem(password, repeated);
    if (problem) {
      setError(problem);
      return;
    }
    setError(null);
    reset.mutate(password);
  }

  return (
    <form
      onSubmit={onSubmit}
      noValidate
      aria-label={`Reset the password of ${user.username}`}
      className="mt-3 grid gap-3 border-t border-slate-200 pt-3 md:grid-cols-2 dark:border-slate-800"
    >
      <NewPasswordFields
        idPrefix={`reset-${user.id}`}
        password={password}
        repeated={repeated}
        onPassword={setPassword}
        onRepeated={setRepeated}
      />
      <div className="flex flex-col gap-2 md:col-span-2">
        {error && (
          <p role="alert" className={alertError}>
            {error}
          </p>
        )}
        <div className="flex flex-wrap gap-2">
          <button type="submit" disabled={reset.isPending} className={buttonPrimary}>
            {reset.isPending ? "Saving…" : "Set new password"}
          </button>
          <button type="button" onClick={() => onDone("")} className={buttonSecondary}>
            Cancel
          </button>
        </div>
      </div>
    </form>
  );
}

function UserRow({ user, isMe }: { user: UserInfo; isMe: boolean }) {
  const api = useApiClient();
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const { reload } = useSession();
  const [mode, setMode] = useState<"idle" | "reset" | "confirm-delete">("idle");
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const refresh = () => queryClient.invalidateQueries({ queryKey: USERS_KEY });
  const otherRole: Role = user.role === "admin" ? "viewer" : "admin";

  const changeRole = useMutation({
    mutationFn: () => api.setUserRole(user.id, otherRole),
    onSuccess: async (updated) => {
      if (isMe && updated.role !== "admin") {
        // You are no longer an admin: this page isn't yours any more.
        await navigate("/", { replace: true });
        reload();
        return;
      }
      setNotice(`${user.username} is now ${ROLE_LABELS[updated.role].toLowerCase()}.`);
      await refresh();
    },
    onError: (e) => setError(errorText(e)),
  });
  const remove = useMutation({
    mutationFn: () => api.deleteUser(user.id),
    onSuccess: refresh,
    onError: (e) => {
      setMode("idle");
      setError(errorText(e));
    },
  });
  const busy = changeRole.isPending || remove.isPending;

  function start(next: typeof mode) {
    setError(null);
    setNotice(null);
    setMode(next);
  }

  return (
    <li className="rounded-lg border border-slate-200 bg-white p-4 dark:border-slate-800 dark:bg-slate-900">
      <div className="flex flex-col gap-3 md:flex-row md:items-start md:justify-between">
        <div className="flex min-w-0 flex-col gap-1">
          <span className="flex flex-wrap items-center gap-2">
            <span className="font-medium break-all">{user.username}</span>
            <span className={`${badge} ${ROLE_STYLES[user.role]}`}>
              <span className="sr-only">Role: </span>
              {ROLE_LABELS[user.role]}
            </span>
            {isMe && <span className="text-xs text-slate-500 dark:text-slate-400">(you)</span>}
          </span>
          <span className="text-xs text-slate-600 dark:text-slate-400">
            Added {formatTime(user.created_at)} · Last login {formatTime(user.last_login_at)}
          </span>
        </div>
        <div className="flex flex-wrap gap-2 md:justify-end">
          {mode === "confirm-delete" ? (
            <>
              <button
                type="button"
                disabled={busy}
                onClick={() => remove.mutate()}
                className={buttonDanger}
                aria-label={`Confirm deleting ${user.username}`}
              >
                Confirm delete
              </button>
              <button type="button" onClick={() => start("idle")} className={buttonSecondary}>
                Keep
              </button>
            </>
          ) : (
            <>
              <button
                type="button"
                disabled={busy}
                onClick={() => {
                  start("idle");
                  changeRole.mutate();
                }}
                className={buttonSecondary}
                aria-label={`Make ${user.username} ${ROLE_LABELS[otherRole].toLowerCase()}`}
              >
                {changeRole.isPending ? "Saving…" : `Make ${ROLE_LABELS[otherRole].toLowerCase()}`}
              </button>
              {isMe ? (
                <Link to="/account" className={buttonSecondary}>
                  Change my password
                </Link>
              ) : (
                <>
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => start(mode === "reset" ? "idle" : "reset")}
                    className={buttonSecondary}
                    aria-label={`Reset the password of ${user.username}`}
                    aria-expanded={mode === "reset"}
                  >
                    Reset password
                  </button>
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => start("confirm-delete")}
                    className={buttonSecondary}
                    aria-label={`Delete ${user.username}`}
                  >
                    Delete
                  </button>
                </>
              )}
            </>
          )}
        </div>
      </div>
      {mode === "confirm-delete" && (
        <p className="mt-2 text-sm text-slate-600 dark:text-slate-400">
          {user.username} is logged out and can't log in again. The audit log keeps their entries.
        </p>
      )}
      {mode === "reset" && (
        <ResetPasswordForm
          user={user}
          onDone={(message) => {
            setMode("idle");
            setNotice(message || null);
          }}
        />
      )}
      {error && (
        <p role="alert" className={`${alertError} mt-3`}>
          {error}
        </p>
      )}
      {notice && (
        <p role="status" className={`${alertInfo} mt-3`}>
          {notice}
        </p>
      )}
    </li>
  );
}

/** Every user, with their role. Admins add users, change roles, reset passwords, delete. */
export function UsersPage() {
  const me = useUser();
  const users = useUsers();

  let list;
  if (users.isPending) {
    list = (
      <p role="status" className="text-sm text-slate-600 dark:text-slate-400">
        Loading users…
      </p>
    );
  } else if (users.isError) {
    list = (
      <div role="alert" className={`${alertError} flex items-center justify-between gap-3`}>
        <span>Could not load the users: {errorText(users.error)}</span>
        <button type="button" className={buttonSecondary} onClick={() => void users.refetch()}>
          Try again
        </button>
      </div>
    );
  } else {
    list = (
      <ul aria-label="Users" className="space-y-3">
        {users.data.map((user) => (
          <UserRow key={user.id} user={user} isMe={user.id === me.id} />
        ))}
      </ul>
    );
  }

  return (
    <>
      <PageHeader title="Users" />
      <p className="mb-4 text-sm text-slate-600 dark:text-slate-400">
        Admins can change devices and manage users; viewers can only look. There is always at least
        one admin: make another user an admin before demoting or deleting the last one.
      </p>
      <div className="space-y-6">
        {list}
        <CreateUserForm />
      </div>
    </>
  );
}
