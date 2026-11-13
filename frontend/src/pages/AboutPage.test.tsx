import { screen, within } from "@testing-library/react";
import { exampleMe, fakeBackend, json, mockSystemTheme, renderApp } from "../test-utils";

const web = vi.hoisted(() => ({ version: "dev" }));
vi.mock("../app/version", async (original) => {
  const real = await original<typeof import("../app/version")>();
  return {
    ...real,
    get FRONTEND_VERSION() {
      return web.version;
    },
  };
});

const ABOUT = {
  backend_version: "0.1.1",
  backend_build_date: "2026-11-14",
  schema_version: "0015",
  prometheus_version: "2.55.1",
  started_at: new Date(Date.now() - 3 * 3600_000).toISOString(),
};

beforeEach(() => {
  mockSystemTheme(false);
  web.version = "dev";
});

function backend(role: "admin" | "viewer", server: string, about: Response | null = null) {
  return fakeBackend({
    session: { ...exampleMe(role), server_version: server },
    routes: { "GET /about": () => about ?? json(ABOUT) },
  });
}

const MISMATCH = /are different versions/;

describe("version in the UI", () => {
  it("shows the version in the sidebar footer", async () => {
    web.version = "0.1.1";
    renderApp("/", backend("admin", "0.1.1"));
    const link = await screen.findByRole("link", { name: "Vantage 0.1.1" });
    expect(link).toHaveAttribute("href", "/settings/about");
    expect(screen.queryByText(/version mismatch/)).not.toBeInTheDocument();
  });

  it("marks a mismatch in the footer and warns on the About page", async () => {
    web.version = "0.1.0";
    renderApp("/settings/about", backend("admin", "0.1.1"));
    expect(await screen.findByRole("alert")).toHaveTextContent(MISMATCH);
    expect(screen.getByText(/version mismatch, see About/)).toBeInTheDocument();
  });

  it("does not warn when either side is dev, or the versions match", async () => {
    web.version = "dev";
    const first = renderApp("/settings/about", backend("admin", "0.1.1"));
    await screen.findByText("2.55.1");
    expect(screen.queryByText(MISMATCH)).not.toBeInTheDocument();
    first.unmount();

    web.version = "0.1.1";
    const second = renderApp("/settings/about", backend("admin", "dev"));
    await screen.findByText("2.55.1");
    expect(screen.queryByText(MISMATCH)).not.toBeInTheDocument();
    second.unmount();

    renderApp("/settings/about", backend("admin", "0.1.1"));
    await screen.findByText("2.55.1");
    expect(screen.queryByText(MISMATCH)).not.toBeInTheDocument();
  });
});

describe("About page", () => {
  it("is reachable by a viewer, without the admin pages", async () => {
    web.version = "0.1.1";
    renderApp("/settings", backend("viewer", "0.1.1"));
    expect(await screen.findByRole("heading", { level: 1, name: "About" })).toBeInTheDocument();
    expect(await screen.findByText("2.55.1")).toBeInTheDocument();
    expect(screen.getByText(/0015/)).toBeInTheDocument();
    expect(screen.getByText(/up 3 h/)).toBeInTheDocument();
    const nav = screen.getByRole("navigation", { name: "Settings sections" });
    expect(within(nav).queryByRole("link", { name: "Users" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Changelog for this version" })).toHaveAttribute(
      "href",
      "https://github.com/levi-xy/vantage/blob/v0.1.1/CHANGELOG.md",
    );
  });

  it("still keeps admin pages closed to a viewer", async () => {
    renderApp("/settings/users", backend("viewer", "0.1.1"));
    expect(await screen.findByRole("heading", { name: "Admins only" })).toBeInTheDocument();
  });

  it("shows unknown for a backend without /about", async () => {
    web.version = "0.1.1";
    renderApp("/settings/about", backend("admin", "0.1.1", json({ detail: "not found" }, 404)));
    expect(await screen.findAllByText("unknown")).not.toHaveLength(0);
    // The web app's and the server's own versions still show (from the bundle and /me).
    expect(screen.getAllByText(/^0\.1\.1$/, { selector: "dd" })).toHaveLength(2);
  });

  it("links the releases list for a dev build", async () => {
    renderApp("/settings/about", backend("admin", "dev"));
    expect(await screen.findByRole("link", { name: "Changelog for this version" })).toHaveAttribute(
      "href",
      "https://github.com/levi-xy/vantage/releases",
    );
  });
});
