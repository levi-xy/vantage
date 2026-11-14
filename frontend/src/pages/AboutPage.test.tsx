import { screen, within } from "@testing-library/react";
import { exampleMe, fakeBackend, json, mockSystemTheme, renderApp } from "../test-utils";

// The web app's baked build (vite `define`), switched per test.
const web = vi.hoisted(() => ({ version: "dev", commit: "" }));
vi.mock("../app/version", async (original) => {
  const real = await original<typeof import("../app/version")>();
  return {
    ...real,
    get FRONTEND_VERSION() {
      return web.version;
    },
    get FRONTEND_COMMIT() {
      return web.commit;
    },
  };
});

const RC_COMMIT = "0123456789abcdef0123456789abcdef01234567";
const OTHER_COMMIT = "89abcdef0123456789abcdef0123456789abcdef";

interface Server {
  version?: string;
  build?: string;
  commit?: string | null;
}

function aboutBody(server: Server) {
  return {
    backend_version: server.version ?? "dev",
    backend_build_version: server.build ?? server.version ?? "dev",
    backend_commit: server.commit ?? null,
    backend_build_date: "2026-11-14",
    schema_version: "0015",
    prometheus_version: "2.55.1",
    // A minute's margin: the page reads the clock before this answer is built.
    started_at: new Date(Date.now() - 3 * 3600_000 - 60_000).toISOString(),
  };
}

beforeEach(() => {
  mockSystemTheme(false);
  web.version = "dev";
  web.commit = "";
});

function backend(role: "admin" | "viewer", server: Server, about: Response | null = null) {
  const session = {
    ...exampleMe(role),
    server_version: server.version ?? "dev",
    server_commit: server.commit ?? null,
  };
  return fakeBackend({
    session,
    routes: { "GET /about": () => about ?? json(aboutBody(server)) },
  });
}

const MISMATCH = /are different builds/;

describe("version in the UI", () => {
  it("a promoted final release shows its release version, built as the rc, with no warning", async () => {
    // The rc's images: both baked as 0.1.1-rc.1 from one commit; the compose file's
    // VANTAGE_RELEASE makes the server report 0.1.1.
    web.version = "0.1.1-rc.1";
    web.commit = RC_COMMIT;
    renderApp(
      "/settings/about",
      backend("admin", { version: "0.1.1", build: "0.1.1-rc.1", commit: RC_COMMIT }),
    );
    expect(await screen.findByText(/Built as 0\.1\.1-rc\.1/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Vantage 0.1.1" })).toHaveAttribute(
      "href",
      "/settings/about",
    );
    expect(screen.queryByText(MISMATCH)).not.toBeInTheDocument();
    expect(screen.queryByText(/version mismatch/)).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Changelog for this version" })).toHaveAttribute(
      "href",
      "https://github.com/levi-xy/vantage/blob/v0.1.1/CHANGELOG.md#v0.1.1",
    );
  });

  it("warns when the web app and the server come from different commits", async () => {
    web.version = "0.1.1";
    web.commit = OTHER_COMMIT;
    renderApp("/settings/about", backend("admin", { version: "0.1.1", commit: RC_COMMIT }));
    expect(await screen.findByRole("alert")).toHaveTextContent(MISMATCH);
    expect(screen.getByText(/version mismatch, see About/)).toBeInTheDocument();
  });

  it("does not warn when either commit is unknown", async () => {
    web.commit = "";
    const first = renderApp("/settings/about", backend("admin", { commit: RC_COMMIT }));
    await screen.findByText("2.55.1");
    expect(screen.queryByText(MISMATCH)).not.toBeInTheDocument();
    first.unmount();

    web.commit = RC_COMMIT;
    renderApp("/settings/about", backend("admin", { commit: null }));
    await screen.findByText("2.55.1");
    expect(screen.queryByText(MISMATCH)).not.toBeInTheDocument();
  });

  it("shows no Built as when the build is the release", async () => {
    renderApp("/settings/about", backend("admin", { version: "0.1.1", commit: RC_COMMIT }));
    await screen.findByText("2.55.1");
    expect(screen.queryByText(/Built as/)).not.toBeInTheDocument();
  });

  it("falls back to the web app's own version for a server that reports none", async () => {
    web.version = "0.1.1";
    // A backend older than 0.1.1: /me has neither field.
    const old = exampleMe("admin");
    delete old.server_version;
    delete old.server_commit;
    renderApp(
      "/",
      fakeBackend({
        session: old,
        routes: { "GET /about": () => json({ detail: "not found" }, 404) },
      }),
    );
    expect(await screen.findByRole("link", { name: "Vantage 0.1.1" })).toBeInTheDocument();
  });
});

describe("About page", () => {
  it("is reachable by a viewer, without the admin pages", async () => {
    renderApp("/settings", backend("viewer", { version: "0.1.1", commit: RC_COMMIT }));
    expect(await screen.findByRole("heading", { level: 1, name: "About" })).toBeInTheDocument();
    expect(await screen.findByText("2.55.1")).toBeInTheDocument();
    expect(screen.getByText(/0015/)).toBeInTheDocument();
    expect(screen.getByText(/up 3 h/)).toBeInTheDocument();
    expect(screen.getByText(RC_COMMIT.slice(0, 12))).toBeInTheDocument();
    const nav = screen.getByRole("navigation", { name: "Settings sections" });
    expect(within(nav).queryByRole("link", { name: "Users" })).not.toBeInTheDocument();
  });

  it("still keeps admin pages closed to a viewer", async () => {
    renderApp("/settings/users", backend("viewer", { version: "0.1.1" }));
    expect(await screen.findByRole("heading", { name: "Admins only" })).toBeInTheDocument();
  });

  it("shows unknown for a backend without /about", async () => {
    renderApp(
      "/settings/about",
      backend("admin", { version: "0.1.1" }, json({ detail: "not found" }, 404)),
    );
    expect(await screen.findAllByText("unknown")).not.toHaveLength(0);
    expect(screen.getByText("0.1.1", { selector: "dd" })).toBeInTheDocument();
  });

  it("links the releases list for a dev build", async () => {
    renderApp("/settings/about", backend("admin", {}));
    expect(await screen.findByRole("link", { name: "Changelog for this version" })).toHaveAttribute(
      "href",
      "https://github.com/levi-xy/vantage/releases",
    );
  });
});
