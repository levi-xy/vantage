// Who is logged in, and the transitions between first-run setup, login and logout.
//
// The session itself is an httpOnly cookie the browser can't read. What the app keeps is
// the user's id, name and role (React state) and the CSRF token (inside the ApiClient,
// in memory only). Neither is written to web storage.

import { useQueryClient } from "@tanstack/react-query";
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import type { ReactNode } from "react";
import { ApiError } from "../api";
import type { ApiClient, Credentials, Me } from "../api";

/** The current user, without the CSRF token (that stays inside the ApiClient). */
export type SessionUser = Omit<Me, "csrf_token">;

export type SessionState =
  /** Startup: waiting for `GET /me` (and `GET /setup/status` when that says 401). */
  | { status: "loading" }
  /** The backend didn't answer, or answered with an unexpected error. */
  | { status: "unreachable"; message: string }
  /** No admin exists yet: the first-run wizard. */
  | { status: "setup" }
  /** Logged out. `notice` explains why, when it wasn't the user's choice. */
  | { status: "anonymous"; notice?: string }
  | { status: "authenticated"; user: SessionUser };

export interface SessionContextValue {
  state: SessionState;
  /** Throws the ApiError on failure (wrong credentials, rate limit...) for the form to show. */
  login: (credentials: Credentials) => Promise<void>;
  /** Create the first admin; the backend logs them in. Throws on failure. */
  setup: (credentials: Credentials) => Promise<void>;
  /** Throws if the backend couldn't end the session; the user then stays logged in. */
  logout: () => Promise<void>;
  /** Ask the backend again (after "unreachable"). */
  reload: () => void;
}

export const SESSION_ENDED = "Your session has ended. Log in again.";
export const SETUP_ALREADY_DONE = "Setup has already been completed. Log in instead.";

const SessionContext = createContext<SessionContextValue | null>(null);

function authenticated(me: Me): SessionState {
  return { status: "authenticated", user: { id: me.id, username: me.username, role: me.role } };
}

function unreachable(error: unknown): SessionState {
  const message =
    error instanceof ApiError && error.status === 0
      ? "Could not reach the Vantage backend."
      : "The Vantage backend answered with an error.";
  return { status: "unreachable", message };
}

/** Where the app stands at startup. `GET /me` runs first, so a reload gets its CSRF token back. */
async function initialState(client: ApiClient): Promise<SessionState> {
  try {
    return authenticated(await client.me());
  } catch (error) {
    if (!(error instanceof ApiError && error.status === 401)) return unreachable(error);
  }
  try {
    const { setup_required } = await client.setupStatus();
    return setup_required ? { status: "setup" } : { status: "anonymous" };
  } catch (error) {
    return unreachable(error);
  }
}

export function SessionProvider({ client, children }: { client: ApiClient; children: ReactNode }) {
  const queryClient = useQueryClient();
  const [state, setState] = useState<SessionState>({ status: "loading" });
  // Only the latest startup check may set the state (StrictMode runs effects twice; reload
  // can be clicked while a check is still out).
  const generation = useRef(0);

  const load = useCallback(async () => {
    const mine = ++generation.current;
    const next = await initialState(client);
    if (mine === generation.current) setState(next);
  }, [client]);

  useEffect(() => {
    void load();
    // The ref object itself (not a DOM node): bumping it on cleanup cancels this check.
    const counter = generation;
    return () => {
      counter.current++;
    };
  }, [load]);

  // Any 401 outside the login form means the session is gone (expired, or ended elsewhere).
  // Drop cached data so nothing from it outlives the session, and go back to the login page.
  useEffect(
    () =>
      client.addUnauthorizedListener(() => {
        queryClient.clear();
        setState((s) =>
          s.status === "authenticated" ? { status: "anonymous", notice: SESSION_ENDED } : s,
        );
      }),
    [client, queryClient],
  );

  const login = useCallback(
    async (credentials: Credentials) => {
      const me = await client.login(credentials);
      generation.current++;
      queryClient.clear();
      setState(authenticated(me));
    },
    [client, queryClient],
  );

  const setup = useCallback(
    async (credentials: Credentials) => {
      let me: Me;
      try {
        me = await client.setup(credentials);
      } catch (error) {
        // Someone else finished setup first: the wizard is closed for good.
        if (error instanceof ApiError && error.status === 409) {
          generation.current++;
          setState({ status: "anonymous", notice: SETUP_ALREADY_DONE });
        }
        throw error;
      }
      generation.current++;
      queryClient.clear();
      setState(authenticated(me));
    },
    [client, queryClient],
  );

  const logout = useCallback(async () => {
    try {
      await client.logout();
    } catch (error) {
      // 401: already logged out on the server, which is what we wanted.
      if (!(error instanceof ApiError && error.status === 401)) {
        // The cookie may still be valid. The client dropped the CSRF token anyway, so get
        // it back, or the user couldn't save anything (a 401 here logs them out instead).
        await client.me().catch(() => undefined);
        throw error;
      }
    }
    generation.current++;
    queryClient.clear();
    setState({ status: "anonymous" });
  }, [client, queryClient]);

  const reload = useCallback(() => {
    setState({ status: "loading" });
    void load();
  }, [load]);

  const value = useMemo(
    () => ({ state, login, setup, logout, reload }),
    [state, login, setup, logout, reload],
  );
  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}

// eslint-disable-next-line react-refresh/only-export-components
export function useSession(): SessionContextValue {
  const value = useContext(SessionContext);
  if (!value) throw new Error("useSession must be used inside <SessionProvider>");
  return value;
}

/** The logged-in user. Only for components rendered behind the authenticated gate. */
// eslint-disable-next-line react-refresh/only-export-components
export function useUser(): SessionUser {
  const { state } = useSession();
  if (state.status !== "authenticated") throw new Error("useUser needs a logged-in user");
  return state.user;
}
