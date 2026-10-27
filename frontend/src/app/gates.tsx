// Route guards: each page is shown only in the session state it belongs to.
// These decide what the UI shows; the backend still enforces every role check itself.

import { Navigate, useLocation } from "react-router";
import type { ReactNode } from "react";
import { AuthScreen } from "../components/AuthScreen";
import { PageHeader } from "../components/PageHeader";
import { buttonPrimary } from "../components/styles";
import type { Role } from "../api";
import { returnPath } from "./returnPath";
import { useSession, useUser } from "./session";

function Loading() {
  return (
    <AuthScreen title="Vantage">
      <p role="status" className="text-sm text-slate-600 dark:text-slate-400">
        Loading…
      </p>
    </AuthScreen>
  );
}

function Unreachable({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <AuthScreen title="Can't load Vantage">
      <p role="alert" className="mb-4 text-sm text-slate-700 dark:text-slate-300">
        {message}
      </p>
      <button type="button" onClick={onRetry} className={buttonPrimary}>
        Try again
      </button>
    </AuthScreen>
  );
}

type GateFor = "authenticated" | "anonymous" | "setup";

/**
 * Renders `children` only in the session state `only`; anything else is redirected to the
 * page for that state. Nothing behind the gate renders (or fetches) before `GET /me` has
 * answered, so every write is sent with the CSRF token it returned.
 */
export function SessionGate({ only, children }: { only: GateFor; children: ReactNode }) {
  const { state, reload } = useSession();
  const location = useLocation();

  if (state.status === "loading") return <Loading />;
  if (state.status === "unreachable")
    return <Unreachable message={state.message} onRetry={reload} />;
  if (state.status === only) return children;

  switch (state.status) {
    case "setup":
      return <Navigate to="/setup" replace />;
    case "authenticated":
      return <Navigate to={returnPath(location.state)} replace />;
    case "anonymous": {
      const from = `${location.pathname}${location.search}${location.hash}`;
      // From the wizard there's nothing to come back to, and after an explicit logout the
      // next person to log in shouldn't land on the previous user's page.
      const keep = only === "authenticated" && !state.loggedOut;
      return <Navigate to="/login" replace state={keep ? { from } : undefined} />;
    }
  }
}

/** Shown to a logged-in user whose role doesn't allow the page. */
function NotAllowed() {
  return (
    <>
      <PageHeader title="Admins only" />
      <p className="text-sm text-slate-600 dark:text-slate-400">
        Your account can view the dashboard but not this page. Ask an admin if you need it.
      </p>
    </>
  );
}

export function RequireRole({ role, children }: { role: Role; children: ReactNode }) {
  const user = useUser();
  return user.role === role ? children : <NotAllowed />;
}
