import { useState } from "react";
import type { FormEvent } from "react";
import { ApiError } from "../api";
import { useSession } from "../app/session";
import { AuthScreen } from "../components/AuthScreen";
import { alertError, alertInfo, buttonPrimary, input, label } from "../components/styles";

function loginError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 401) return "Wrong username or password.";
    if (error.status === 429) return "Too many failed logins. Wait a few minutes and try again.";
    if (error.status === 0) return "Could not reach the Vantage backend.";
  }
  return "Login failed. Try again.";
}

export function LoginPage() {
  const { state, login } = useSession();
  const notice = state.status === "anonymous" ? state.notice : undefined;
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy) return;
    if (!username || !password) {
      setError("Enter your username and password.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      // On success the session gate redirects to where the user was going.
      await login({ username, password });
    } catch (err) {
      setError(loginError(err));
      setPassword("");
      setBusy(false);
    }
  }

  return (
    <AuthScreen title="Log in">
      <form onSubmit={onSubmit} noValidate className="space-y-4">
        {notice && !error && (
          <p role="status" className={alertInfo}>
            {notice}
          </p>
        )}
        {error && (
          <p role="alert" className={alertError}>
            {error}
          </p>
        )}
        <div>
          <label htmlFor="login-username" className={label}>
            Username
          </label>
          <input
            id="login-username"
            name="username"
            autoComplete="username"
            autoCapitalize="none"
            spellCheck={false}
            maxLength={64}
            required
            className={input}
            value={username}
            onChange={(e) => setUsername(e.target.value)}
          />
        </div>
        <div>
          <label htmlFor="login-password" className={label}>
            Password
          </label>
          <input
            id="login-password"
            name="password"
            type="password"
            autoComplete="current-password"
            maxLength={1024}
            required
            className={input}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </div>
        <button type="submit" disabled={busy} className={`${buttonPrimary} w-full`}>
          {busy ? "Logging in…" : "Log in"}
        </button>
      </form>
    </AuthScreen>
  );
}
