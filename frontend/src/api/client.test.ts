import { ApiClient, ApiError, API_BASE, CSRF_HEADER } from "./client";
import type { Me } from "./types";

const ME: Me = { id: 1, username: "admin", role: "admin", csrf_token: "example-csrf-token" };

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function setup(...responses: Response[]) {
  const fetchMock = vi.fn<typeof fetch>();
  for (const r of responses) fetchMock.mockResolvedValueOnce(r);
  const onUnauthorized = vi.fn();
  const client = new ApiClient({ fetch: fetchMock, onUnauthorized });
  return { client, fetchMock, onUnauthorized };
}

function call(fetchMock: ReturnType<typeof vi.fn<typeof fetch>>, n = 0) {
  const args = fetchMock.mock.calls[n];
  if (!args) throw new Error(`fetch call ${n} not made`);
  const [url, init] = args;
  return { url, init: init ?? {}, headers: (init?.headers ?? {}) as Record<string, string> };
}

/** A client that already holds a session (CSRF token from GET /me). */
async function loggedIn(...responses: Response[]) {
  const ctx = setup(json(ME), ...responses);
  await ctx.client.me();
  return ctx;
}

describe("ApiClient requests", () => {
  it("sends every request to the same-origin /api/v1 base with cookies", async () => {
    const { client, fetchMock } = setup(json({ setup_required: true }));
    await expect(client.setupStatus()).resolves.toEqual({ setup_required: true });
    const { url, init } = call(fetchMock);
    expect(url).toBe(`${API_BASE}/setup/status`);
    expect(init.method).toBe("GET");
    expect(init.credentials).toBe("same-origin");
    expect(init.redirect).toBe("error");
    expect(init.cache).toBe("no-store");
  });

  it("refuses paths that could leave the backend", async () => {
    const { client, fetchMock } = setup();
    for (const path of ["https://203.0.113.10/x", "//203.0.113.10/x", "me", "/x?u=http://a"]) {
      await expect(client.request("GET", path)).rejects.toThrow(/relative/);
    }
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("sends JSON bodies with a JSON content type", async () => {
    const { client, fetchMock } = setup(json(ME));
    await client.login({ username: "admin", password: "example-password" });
    const { url, init, headers } = call(fetchMock);
    expect(url).toBe(`${API_BASE}/auth/login`);
    expect(init.method).toBe("POST");
    expect(headers["Content-Type"]).toBe("application/json");
    expect(JSON.parse(String(init.body))).toEqual({
      username: "admin",
      password: "example-password",
    });
  });

  it("builds audit paging parameters", async () => {
    const { client, fetchMock } = setup(json([]), json([]));
    await client.audit();
    await client.audit({ limit: 20, before_id: 41 });
    expect(call(fetchMock, 0).url).toBe(`${API_BASE}/audit`);
    expect(call(fetchMock, 1).url).toBe(`${API_BASE}/audit?limit=20&before_id=41`);
  });

  it("lists integrations", async () => {
    const types = [{ type_id: "ping", name: "Ping", category: "generic", schema: {} }];
    const { client, fetchMock } = setup(json(types));
    await expect(client.integrations()).resolves.toEqual(types);
    expect(call(fetchMock).url).toBe(`${API_BASE}/integrations`);
  });

  it("creates the first admin through setup", async () => {
    const { client, fetchMock } = setup(json(ME, 201));
    await expect(
      client.setup({ username: "admin", password: "example-password" }),
    ).resolves.toEqual(ME);
    expect(call(fetchMock).url).toBe(`${API_BASE}/setup`);
    expect(client.hasSession).toBe(true);
  });
});

describe("ApiClient CSRF", () => {
  it.each(["POST", "PUT", "PATCH", "DELETE"] as const)(
    "sends the token from the session on %s",
    async (method) => {
      const { client, fetchMock } = await loggedIn(json({}));
      await client.request(method, "/dashboards/1", method === "DELETE" ? undefined : {});
      expect(call(fetchMock, 1).headers[CSRF_HEADER]).toBe(ME.csrf_token);
    },
  );

  it("does not send the token on GET", async () => {
    const { client, fetchMock } = await loggedIn(json([]));
    await client.integrations();
    expect(call(fetchMock, 1).headers[CSRF_HEADER]).toBeUndefined();
  });

  it.each([
    ["login", (c: ApiClient) => c.login({ username: "admin", password: "example-password" })],
    ["setup", (c: ApiClient) => c.setup({ username: "admin", password: "example-password" })],
  ])("takes the token from the %s response", async (_name, start) => {
    const { client, fetchMock } = setup(json(ME), json({}));
    await start(client);
    await client.request("PUT", "/dashboards/1", {});
    expect(call(fetchMock, 1).headers[CSRF_HEADER]).toBe(ME.csrf_token);
  });

  it("forgets the token on logout", async () => {
    const { client, fetchMock } = await loggedIn(new Response(null, { status: 204 }), json({}));
    await expect(client.logout()).resolves.toBeUndefined();
    expect(call(fetchMock, 1).url).toBe(`${API_BASE}/auth/logout`);
    expect(call(fetchMock, 1).headers[CSRF_HEADER]).toBe(ME.csrf_token);
    expect(client.hasSession).toBe(false);
    await client.request("POST", "/x", {});
    expect(call(fetchMock, 2).headers[CSRF_HEADER]).toBeUndefined();
  });

  it("forgets the token even when logout fails", async () => {
    const { client } = await loggedIn(json({ detail: "server error" }, 500));
    await expect(client.logout()).rejects.toBeInstanceOf(ApiError);
    expect(client.hasSession).toBe(false);
  });

  it("never writes the token or session data to web storage", async () => {
    localStorage.clear();
    sessionStorage.clear();
    const { client } = await loggedIn(json({}));
    await client.request("POST", "/x", {});
    for (const storage of [localStorage, sessionStorage]) {
      expect(storage.length).toBe(0);
    }
    expect(document.cookie).not.toContain(ME.csrf_token);
  });
});

describe("ApiClient errors", () => {
  it("on 401 clears the session, notifies, and throws", async () => {
    const { client, fetchMock, onUnauthorized } = await loggedIn(
      json({ detail: "not logged in" }, 401),
      json({}),
    );
    const err = await client.integrations().catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err).toMatchObject({ status: 401, message: "not logged in" });
    expect(onUnauthorized).toHaveBeenCalledOnce();
    expect(client.hasSession).toBe(false);
    await client.request("POST", "/x", {});
    expect(call(fetchMock, 2).headers[CSRF_HEADER]).toBeUndefined();
  });

  it("does not treat 403 as logged out", async () => {
    const { client, onUnauthorized } = await loggedIn(json({ detail: "admin role required" }, 403));
    await expect(client.audit()).rejects.toMatchObject({
      status: 403,
      message: "admin role required",
    });
    expect(onUnauthorized).not.toHaveBeenCalled();
    expect(client.hasSession).toBe(true);
  });

  it("joins validation error messages", async () => {
    const detail = [
      { loc: ["body", "username"], msg: "String should match pattern", type: "x" },
      { loc: ["body", "password"], msg: "String too short", type: "y" },
    ];
    const { client } = setup(json({ detail }, 422));
    await expect(client.setup({ username: "-", password: "x" })).rejects.toMatchObject({
      status: 422,
      message: "String should match pattern; String too short",
    });
    expect(client.hasSession).toBe(false);
  });

  it("falls back to the status when the body isn't JSON", async () => {
    const { client } = setup(new Response("<html>bad gateway</html>", { status: 502 }));
    await expect(client.me()).rejects.toMatchObject({
      status: 502,
      message: "Request failed (502)",
    });
  });

  it("reports network failures as status 0 without the underlying error", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockRejectedValue(new TypeError("example failure"));
    const client = new ApiClient({ fetch: fetchMock });
    await expect(client.me()).rejects.toMatchObject({
      status: 0,
      message: "Could not reach the Vantage backend",
    });
  });
});
