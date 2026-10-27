// Typed client for the Vantage backend. The browser talks only to the backend
// (AGENTS.md hard rule 4), so every request goes to a same-origin /api/v1 path.

import type {
  AuditEntry,
  AuditQuery,
  Credentials,
  Instance,
  InstanceCreate,
  InstanceTest,
  InstanceUpdate,
  IntegrationInfo,
  Me,
  SetupStatus,
  TestResult,
} from "./types";

export const API_BASE = "/api/v1";
export const CSRF_HEADER = "X-CSRF-Token";

type Method = "GET" | "POST" | "PUT" | "PATCH" | "DELETE";

/** A non-2xx response, or a request that never got one. */
export class ApiError extends Error {
  /** HTTP status; 0 when the request failed before a response arrived. */
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

export interface ApiClientOptions {
  fetch?: typeof fetch;
  /**
   * Called whenever the backend answers 401 (no session, or it expired), except on a
   * login attempt: there a 401 means wrong credentials, and the login form reports it.
   */
  onUnauthorized?: () => void;
}

// A 401 from these paths is a failed credential check, not an ended session, so it
// doesn't notify the unauthorized listeners (which send the user to the login page).
const CREDENTIAL_PATHS: ReadonlySet<string> = new Set(["/auth/login"]);

/** Turn FastAPI's `{"detail": ...}` error body into one readable line. */
function errorMessage(body: unknown, fallback: string): string {
  if (typeof body !== "object" || body === null || !("detail" in body)) return fallback;
  const { detail } = body;
  if (typeof detail === "string" && detail) return detail;
  if (Array.isArray(detail)) {
    const msgs = detail
      .map((d: unknown) =>
        typeof d === "object" && d !== null && "msg" in d && typeof d.msg === "string" ? d.msg : "",
      )
      .filter(Boolean);
    if (msgs.length) return msgs.join("; ");
  }
  return fallback;
}

export class ApiClient {
  readonly #fetch: typeof fetch;
  readonly #unauthorizedListeners = new Set<() => void>();
  // In memory only: never written to localStorage/sessionStorage (AGENTS.md frontend rules).
  #csrfToken: string | null = null;

  constructor(options: ApiClientOptions = {}) {
    this.#fetch = options.fetch ?? ((input, init) => globalThis.fetch(input, init));
    if (options.onUnauthorized) this.#unauthorizedListeners.add(options.onUnauthorized);
  }

  /** Like the `onUnauthorized` option, added later. Returns a function that removes it. */
  addUnauthorizedListener(listener: () => void): () => void {
    this.#unauthorizedListeners.add(listener);
    return () => {
      this.#unauthorizedListeners.delete(listener);
    };
  }

  /** Whether a session's CSRF token is held (set by setup, login and me). */
  get hasSession(): boolean {
    return this.#csrfToken !== null;
  }

  /** Low-level request. `path` is relative to /api/v1 and must start with a single "/". */
  async request<T>(method: Method, path: string, body?: unknown): Promise<T> {
    if (!path.startsWith("/") || path.startsWith("//") || path.includes("://")) {
      throw new Error(`API path must be relative to ${API_BASE}`);
    }
    const headers: Record<string, string> = { Accept: "application/json" };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (method !== "GET" && this.#csrfToken !== null) headers[CSRF_HEADER] = this.#csrfToken;

    let response: Response;
    try {
      response = await this.#fetch(API_BASE + path, {
        method,
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
        // The session is an httpOnly cookie: sent to our own origin, never anywhere else.
        credentials: "same-origin",
        cache: "no-store",
        // The API never redirects; refuse to follow one rather than carry the cookie along.
        redirect: "error",
      });
    } catch {
      throw new ApiError(0, "Could not reach the Vantage backend");
    }

    if (response.status === 401) {
      // Always: whatever session this token belonged to is no longer usable.
      this.#csrfToken = null;
      if (!CREDENTIAL_PATHS.has(path)) {
        for (const listener of [...this.#unauthorizedListeners]) listener();
      }
    }
    if (response.status === 204) {
      return undefined as T;
    }
    const data: unknown = await response.json().catch(() => null);
    if (!response.ok) {
      throw new ApiError(
        response.status,
        errorMessage(data, `Request failed (${response.status})`),
      );
    }
    return data as T;
  }

  #session(me: Me): Me {
    this.#csrfToken = me.csrf_token;
    return me;
  }

  setupStatus(): Promise<SetupStatus> {
    return this.request("GET", "/setup/status");
  }

  /** Create the first admin; the backend logs them in. */
  async setup(credentials: Credentials): Promise<Me> {
    return this.#session(await this.request<Me>("POST", "/setup", credentials));
  }

  async login(credentials: Credentials): Promise<Me> {
    return this.#session(await this.request<Me>("POST", "/auth/login", credentials));
  }

  async logout(): Promise<void> {
    try {
      await this.request<undefined>("POST", "/auth/logout");
    } finally {
      this.#csrfToken = null;
    }
  }

  async me(): Promise<Me> {
    return this.#session(await this.request<Me>("GET", "/me"));
  }

  /** Admin only. Newest first. */
  audit(query: AuditQuery = {}): Promise<AuditEntry[]> {
    const params = new URLSearchParams();
    if (query.limit !== undefined) params.set("limit", String(query.limit));
    if (query.before_id !== undefined) params.set("before_id", String(query.before_id));
    const qs = params.toString();
    return this.request("GET", qs ? `/audit?${qs}` : "/audit");
  }

  integrations(): Promise<IntegrationInfo[]> {
    return this.request("GET", "/integrations");
  }

  instances(): Promise<Instance[]> {
    return this.request("GET", "/instances");
  }

  instance(id: string): Promise<Instance> {
    return this.request("GET", `/instances/${encodeURIComponent(id)}`);
  }

  /** Admin only. */
  createInstance(body: InstanceCreate): Promise<Instance> {
    return this.request("POST", "/instances", body);
  }

  /** Admin only. Omitted config fields (secrets included) keep their stored values. */
  updateInstance(id: string, body: InstanceUpdate): Promise<Instance> {
    return this.request("PATCH", `/instances/${encodeURIComponent(id)}`, body);
  }

  /** Admin only. */
  deleteInstance(id: string): Promise<void> {
    return this.request("DELETE", `/instances/${encodeURIComponent(id)}`);
  }

  /** Admin only. Tests the saved config. */
  testInstance(id: string): Promise<TestResult> {
    return this.request("POST", `/instances/${encodeURIComponent(id)}/test`);
  }

  /** Admin only. Tests a config that hasn't been saved. */
  testUnsavedInstance(body: InstanceTest): Promise<TestResult> {
    return this.request("POST", "/instances/test", body);
  }
}
