// Typed client for the Vantage backend. The browser talks only to the backend
// (AGENTS.md hard rule 4), so every request goes to a same-origin /api/v1 path.

import type {
  AuditEntry,
  AuditQuery,
  Credentials,
  IntegrationInfo,
  Me,
  SetupStatus,
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
  /** Called whenever the backend answers 401 (no session, or it expired). */
  onUnauthorized?: () => void;
}

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
  readonly #onUnauthorized: (() => void) | undefined;
  // In memory only: never written to localStorage/sessionStorage (AGENTS.md frontend rules).
  #csrfToken: string | null = null;

  constructor(options: ApiClientOptions = {}) {
    this.#fetch = options.fetch ?? ((input, init) => globalThis.fetch(input, init));
    this.#onUnauthorized = options.onUnauthorized;
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
      this.#csrfToken = null;
      this.#onUnauthorized?.();
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
}
