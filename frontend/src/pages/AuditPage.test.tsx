import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import type { AuditEntry } from "../api";
import { exampleMe, fakeBackend, json, renderApp } from "../test-utils";
import { changeLines } from "./auditChanges";

function entry(id: number, overrides: Partial<AuditEntry> = {}): AuditEntry {
  return {
    id,
    user_id: 1,
    action: "update",
    target_type: "instance",
    target_id: `00000000-0000-4000-8000-${String(id).padStart(12, "0")}`,
    at: "2026-01-02T03:04:05+00:00",
    changes: { name: { old: `old-${id}`, new: `new-${id}` } },
    ...overrides,
  };
}

/** Ids `from` down to `from - count + 1`, newest first like the backend. */
function page(from: number, count: number): AuditEntry[] {
  return Array.from({ length: count }, (_, i) => entry(from - i));
}

function auditRequests(backend: ReturnType<typeof fakeBackend>): string[] {
  return backend
    .requests()
    .map((r) => r.route)
    .filter((route) => route.startsWith("GET /audit"));
}

async function findTable() {
  return screen.findByRole("table", { name: "Audit entries, newest first" });
}

describe("audit page", () => {
  it("shows the entries in a table, newest first", async () => {
    const backend = fakeBackend({
      session: exampleMe("admin"),
      routes: {
        "GET /audit": () =>
          json([
            entry(7, {
              action: "login_failed",
              user_id: null,
              target_type: "user",
              target_id: null,
              changes: { username: "example-user" },
            }),
            entry(6, { action: "create", changes: { name: "edge-fw", enabled: true } }),
          ]),
      },
    });
    renderApp("/audit", backend);

    const table = await findTable();
    const [header, failed, created, ...rest] = within(table).getAllByRole("row");
    if (!header || !failed || !created) throw new Error("expected three rows");
    expect(rest).toEqual([]);
    expect(
      within(header)
        .getAllByRole("columnheader")
        .map((h) => h.textContent),
    ).toEqual(["Time", "User", "Action", "Target", "Changes"]);
    expect(within(failed).getByText("login_failed")).toBeInTheDocument();
    expect(within(failed).getByText("—")).toBeInTheDocument();
    expect(within(failed).getByText("example-user")).toBeInTheDocument();
    expect(within(created).getByText("create")).toBeInTheDocument();
    expect(within(created).getByText("User #1")).toBeInTheDocument();
    expect(within(created).getByText("edge-fw")).toBeInTheDocument();
    expect(within(created).getByText("true")).toBeInTheDocument();
    const time = created.querySelector("time");
    expect(time).toHaveAttribute("datetime", "2026-01-02T03:04:05+00:00");

    expect(auditRequests(backend)).toEqual(["GET /audit?limit=50"]);
  });

  it('shows redacted secret fields exactly as "<changed>"', async () => {
    const backend = fakeBackend({
      session: exampleMe("admin"),
      routes: {
        "GET /audit": () =>
          json([
            entry(3, {
              changes: {
                config: {
                  host: { old: "192.0.2.10", new: "192.0.2.11" },
                  api_token: "<changed>",
                },
              },
            }),
            entry(2, {
              action: "create",
              target_type: "user",
              changes: { username: "example-admin", role: "admin", password: "<changed>" },
            }),
          ]),
      },
    });
    renderApp("/audit", backend);
    const table = await findTable();

    const placeholders = within(table).getAllByText("<changed>", { exact: true });
    expect(placeholders).toHaveLength(2);
    expect(within(table).getByText("config.api_token:", { exact: false })).toBeInTheDocument();
    expect(within(table).getByText("password:", { exact: false })).toBeInTheDocument();
    expect(within(table).getByText("192.0.2.10 → 192.0.2.11")).toBeInTheDocument();
    // Rendered as text, not swallowed as an HTML tag.
    expect(table.querySelector("changed")).toBeNull();
  });

  it("renders values as text, never as markup", async () => {
    const markup = '<img src="x" onerror="alert(1)">';
    const backend = fakeBackend({
      session: exampleMe("admin"),
      routes: { "GET /audit": () => json([entry(1, { changes: { name: markup } })]) },
    });
    renderApp("/audit", backend);
    const table = await findTable();
    expect(within(table).getByText(markup)).toBeInTheDocument();
    expect(table.querySelector("img")).toBeNull();
  });

  it("says when there are no entries", async () => {
    const backend = fakeBackend({
      session: exampleMe("admin"),
      routes: { "GET /audit": () => json([]) },
    });
    renderApp("/audit", backend);
    expect(await screen.findByText("No audit entries yet.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Load older entries" })).not.toBeInTheDocument();
  });

  it("reports a failed load and retries on request", async () => {
    let fail = true;
    const backend = fakeBackend({
      session: exampleMe("admin"),
      routes: {
        "GET /audit": () =>
          fail ? json({ detail: "admin role required" }, 403) : json([entry(1)]),
      },
    });
    renderApp("/audit", backend);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Could not load the audit log: admin role required");

    fail = false;
    fireEvent.click(within(alert).getByRole("button", { name: "Try again" }));
    const table = await findTable();
    expect(within(table).getByText("new-1", { exact: false })).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("pages backwards with before_id until a short page", async () => {
    const backend = fakeBackend({
      session: exampleMe("admin"),
      routes: {
        // Override handlers don't see the query string; read it from the request log.
        "GET /audit": () => {
          const last = auditRequests(backend).at(-1) ?? "";
          return json(last.includes("before_id=") ? page(51, 3) : page(100, 50));
        },
      },
    });
    renderApp("/audit", backend);

    const table = await findTable();
    expect(within(table).getAllByRole("row")).toHaveLength(51);

    fireEvent.click(screen.getByRole("button", { name: "Load older entries" }));
    await waitFor(() => expect(within(table).getAllByRole("row")).toHaveLength(54));
    expect(auditRequests(backend)).toEqual([
      "GET /audit?limit=50",
      "GET /audit?limit=50&before_id=51",
    ]);
    // The last page was short: nothing older.
    expect(screen.queryByRole("button", { name: "Load older entries" })).not.toBeInTheDocument();
    expect(within(table).getByText("new-49", { exact: false })).toBeInTheDocument();
  });

  it("keeps loaded entries when an older page fails", async () => {
    const backend = fakeBackend({
      session: exampleMe("admin"),
      routes: {
        "GET /audit": () => {
          const last = auditRequests(backend).at(-1) ?? "";
          return last.includes("before_id=")
            ? json({ detail: "bad request" }, 422)
            : json(page(50, 50));
        },
      },
    });
    renderApp("/audit", backend);
    const table = await findTable();

    fireEvent.click(screen.getByRole("button", { name: "Load older entries" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Could not load older entries: bad request",
    );
    expect(within(table).getAllByRole("row")).toHaveLength(51);
  });

  it("is never requested for a viewer", async () => {
    const backend = fakeBackend({ session: exampleMe("viewer") });
    renderApp("/audit", backend);
    expect(
      await screen.findByRole("heading", { level: 1, name: "Admins only" }),
    ).toBeInTheDocument();
    expect(auditRequests(backend)).toEqual([]);
  });
});

describe("changeLines", () => {
  it("flattens nested fields and formats diffs", () => {
    expect(
      changeLines({
        name: { old: "a", new: "b" },
        site: { old: null, new: "lab" },
        tags: ["x", "y"],
        config: { port: { old: 80, new: 443 }, api_key: "<changed>" },
        empty: {},
      }),
    ).toEqual([
      { path: "name", value: "a → b" },
      { path: "site", value: "null → lab" },
      { path: "tags", value: '["x","y"]' },
      { path: "config.port", value: "80 → 443" },
      { path: "config.api_key", value: "<changed>" },
      { path: "empty", value: "{}" },
    ]);
  });

  it("passes the redaction placeholder through unchanged", () => {
    expect(changeLines({ password: "<changed>", secrets: "<changed>" })).toEqual([
      { path: "password", value: "<changed>" },
      { path: "secrets", value: "<changed>" },
    ]);
  });
});
