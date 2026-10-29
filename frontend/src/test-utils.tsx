import { render } from "@testing-library/react";
import { createMemoryRouter } from "react-router";
import { ApiClient, API_BASE } from "./api";
import type { Me, Role } from "./api";
import { App } from "./app/App";
import { createQueryClient } from "./app/queryClient";
import { routes } from "./app/routes";

/** A controllable `prefers-color-scheme: dark` media query (jsdom has no matchMedia). */
export function mockSystemTheme(dark: boolean) {
  const listeners = new Set<() => void>();
  const query = {
    matches: dark,
    media: "(prefers-color-scheme: dark)",
    addEventListener: (_type: string, fn: () => void) => listeners.add(fn),
    removeEventListener: (_type: string, fn: () => void) => listeners.delete(fn),
  };
  vi.stubGlobal(
    "matchMedia",
    vi.fn(() => query),
  );
  return {
    set(next: boolean) {
      query.matches = next;
      for (const fn of listeners) fn();
    },
    listenerCount: () => listeners.size,
  };
}

export const EXAMPLE_PASSWORD = "example-password";
export const EXAMPLE_CSRF = "example-csrf-token";

export function exampleMe(role: Role = "admin", username = role): Me {
  return { id: role === "admin" ? 1 : 2, username, role, csrf_token: EXAMPLE_CSRF };
}

export function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

export interface FakeBackendOptions {
  /** The user whose session cookie the browser holds at startup; null = none. */
  session?: Me | null;
  setupRequired?: boolean;
  /** Users who can log in, by username (password is EXAMPLE_PASSWORD). */
  users?: Me[];
  /** Override a route: key is "METHOD /path" relative to /api/v1. */
  routes?: Record<string, (body: unknown) => Response | Promise<Response>>;
}

/**
 * An in-memory stand-in for the auth endpoints of the backend. Unknown GETs answer `[]`,
 * unknown writes `{}`; a request without a session answers 401 like the real one.
 */
export function fakeBackend(options: FakeBackendOptions = {}) {
  const state = {
    session: options.session ?? null,
    setupRequired: options.setupRequired ?? false,
  };
  const users = options.users ?? [exampleMe("admin"), exampleMe("viewer")];
  const unauthorized = () => json({ detail: "not authenticated" }, 401);

  const fetchMock = vi.fn<typeof fetch>(async (input, init) => {
    const url = String(input);
    if (!url.startsWith(API_BASE)) throw new Error(`unexpected request to ${url}`);
    const path = url.slice(API_BASE.length).split("?")[0] ?? "";
    const method = init?.method ?? "GET";
    const body: unknown = init?.body ? JSON.parse(String(init.body)) : undefined;
    const override = options.routes?.[`${method} ${path}`];
    if (override) return override(body);

    switch (`${method} ${path}`) {
      case "GET /setup/status":
        return json({ setup_required: state.setupRequired });
      case "POST /setup": {
        if (!state.setupRequired) return json({ detail: "setup has already been completed" }, 409);
        const { username } = body as { username: string };
        state.setupRequired = false;
        state.session = { ...exampleMe("admin"), username };
        return json(state.session, 201);
      }
      case "POST /auth/login": {
        const { username, password } = body as { username: string; password: string };
        const user = users.find((u) => u.username === username);
        if (!user || password !== EXAMPLE_PASSWORD) {
          return json({ detail: "invalid username or password" }, 401);
        }
        state.session = user;
        return json(user);
      }
      case "POST /auth/logout":
        if (!state.session) return unauthorized();
        state.session = null;
        return new Response(null, { status: 204 });
      case "GET /me":
        return state.session ? json(state.session) : unauthorized();
      case "GET /dashboards/1":
        if (!state.session) return unauthorized();
        return json({ id: 1, name: "Default", layout: { cards: [] } });
      case "POST /query":
        if (!state.session) return unauthorized();
        return json({ result_type: "vector", series: [], warnings: [] });
      default:
        if (!state.session) return unauthorized();
        return json(method === "GET" ? [] : {});
    }
  });

  /** Requests made so far, as "METHOD /path" plus the CSRF header they carried. */
  const requests = () =>
    fetchMock.mock.calls.map(([input, init]) => ({
      route: `${init?.method ?? "GET"} ${String(input).slice(API_BASE.length)}`,
      csrf: (init?.headers as Record<string, string> | undefined)?.["X-CSRF-Token"],
    }));

  return { fetch: fetchMock, state, requests };
}

export type FakeBackend = ReturnType<typeof fakeBackend>;

/** Render the whole app at `path`. By default the browser holds an admin's session. */
export function renderApp(
  path = "/",
  backend: FakeBackend = fakeBackend({ session: exampleMe() }),
) {
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  const client = new ApiClient({ fetch: backend.fetch });
  const queryClient = createQueryClient();
  const result = render(<App router={router} queryClient={queryClient} client={client} />);
  return { ...result, router, client, queryClient, backend };
}
