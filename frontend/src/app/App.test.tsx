import { act, fireEvent, screen, within } from "@testing-library/react";
import {
  exampleMe,
  fakeBackend,
  json,
  mockSystemTheme,
  openAccountMenu,
  renderApp,
} from "../test-utils";

/** Render as a logged-in admin and wait until the session check has finished. */
async function renderReady(path: string) {
  const result = renderApp(path);
  await screen.findByRole("navigation", { name: "Main" });
  return result;
}

beforeEach(() => {
  mockSystemTheme(false);
});

describe("routing", () => {
  it.each([
    ["/", "Dashboard"],
    ["/devices", "Devices"],
    ["/settings/audit", "Audit log"],
    ["/settings/users", "Users"],
    ["/account", "My account"],
    ["/no/such/page", "Page not found"],
  ])("renders %s inside the layout", async (path, heading) => {
    renderApp(path);
    expect(await screen.findByRole("heading", { level: 1, name: heading })).toBeInTheDocument();
    expect(screen.getByRole("navigation", { name: "Main" })).toBeInTheDocument();
    expect(screen.getByRole("main")).toBeInTheDocument();
  });

  it("navigates with the main menu and marks the current page", async () => {
    const { router } = await renderReady("/");
    const nav = screen.getByRole("navigation", { name: "Main" });
    expect(within(nav).getByRole("link", { name: "Dashboard" })).toHaveAttribute(
      "aria-current",
      "page",
    );

    fireEvent.click(within(nav).getByRole("link", { name: "Devices" }));
    expect(await screen.findByRole("heading", { level: 1, name: "Devices" })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/devices");
    expect(within(nav).getByRole("link", { name: "Devices" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(within(nav).getByRole("link", { name: "Dashboard" })).not.toHaveAttribute(
      "aria-current",
    );
  });

  it("links back to the dashboard from an unknown page", async () => {
    renderApp("/missing");
    fireEvent.click(await screen.findByRole("link", { name: "Back to the dashboard" }));
    expect(await screen.findByRole("heading", { level: 1, name: "Dashboard" })).toBeInTheDocument();
  });
});

describe("layout", () => {
  it("keeps only My account in the account menu, which closes after navigating", async () => {
    await renderReady("/");
    const button = screen.getByRole("button", { name: "admin (Admin)" });
    expect(button).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("navigation", { name: "Account" })).not.toBeInTheDocument();

    const menu = openAccountMenu();
    expect(button).toHaveAttribute("aria-expanded", "true");
    expect(within(menu).queryByRole("link", { name: "Users" })).not.toBeInTheDocument();
    fireEvent.click(within(menu).getByRole("link", { name: "My account" }));
    expect(
      await screen.findByRole("heading", { level: 1, name: "My account" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("navigation", { name: "Account" })).not.toBeInTheDocument();

    openAccountMenu();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("navigation", { name: "Account" })).not.toBeInTheDocument();
  });

  it("swaps the sidebar for a bottom tab bar on phones, More opening the account menu", async () => {
    await renderReady("/");
    expect(screen.getByRole("navigation", { name: "Main" })).toHaveClass("hidden", "md:flex");
    const tabs = screen.getByRole("navigation", { name: "Tabs" });
    expect(tabs).toHaveClass("md:hidden");
    expect(within(tabs).getByRole("link", { name: "Dashboard" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    fireEvent.click(within(tabs).getByRole("button", { name: "More" }));
    expect(screen.getByRole("navigation", { name: "Account" })).toBeInTheDocument();
  });

  it("hides the sidebar to icons and remembers it", async () => {
    localStorage.clear();
    await renderReady("/");
    const header = screen.getByRole("banner");
    expect(header).toHaveClass("md:w-60");
    fireEvent.click(screen.getByRole("button", { name: "Hide sidebar" }));
    expect(header).toHaveClass("md:w-16");
    // The labels stay for screen readers, so the links keep their names.
    const nav = screen.getByRole("navigation", { name: "Main" });
    expect(within(nav).getByRole("link", { name: "Devices" })).toBeInTheDocument();
    expect(localStorage.getItem("vantage.sidebarCollapsed")).toBe("1");
    fireEvent.click(screen.getByRole("button", { name: "Show sidebar" }));
    expect(header).toHaveClass("md:w-60");
    localStorage.clear();
  });

  it("reorders the main menu with Alt+Arrow and remembers it", async () => {
    localStorage.clear();
    const first = await renderReady("/");
    const names = () =>
      within(screen.getByRole("navigation", { name: "Main" }))
        .getAllByRole("link")
        .map((a) => a.textContent);
    expect(names()).toEqual(["Dashboard", "Devices", "Alerts", "Map", "Settings"]);
    expect(localStorage.getItem("vantage.navOrder")).toBeNull();
    const nav = screen.getByRole("navigation", { name: "Main" });
    fireEvent.keyDown(within(nav).getByRole("link", { name: "Map" }), {
      key: "ArrowUp",
      altKey: true,
    });
    expect(names()).toEqual(["Dashboard", "Devices", "Map", "Alerts", "Settings"]);
    expect(within(nav).getByRole("link", { name: "Map" })).toHaveFocus();
    // The first item can't go further up: nothing changes.
    fireEvent.keyDown(within(nav).getByRole("link", { name: "Dashboard" }), {
      key: "ArrowUp",
      altKey: true,
    });
    expect(JSON.parse(localStorage.getItem("vantage.navOrder") ?? "[]")).toEqual([
      "/",
      "/devices",
      "/map",
      "/alerts",
      "/settings",
    ]);
    first.unmount();
    await renderReady("/");
    expect(names()).toEqual(["Dashboard", "Devices", "Map", "Alerts", "Settings"]);
    localStorage.clear();
  });

  it("ignores a garbage saved menu order", async () => {
    localStorage.setItem("vantage.navOrder", '{"not":"a list"}');
    await renderReady("/");
    const nav = screen.getByRole("navigation", { name: "Main" });
    expect(
      within(nav)
        .getAllByRole("link")
        .map((a) => a.textContent),
    ).toEqual(["Dashboard", "Devices", "Alerts", "Map", "Settings"]);
    localStorage.clear();
  });

  it("gives map pages the whole area next to the sidebar", async () => {
    await renderReady("/map");
    expect(screen.getByRole("main")).toHaveClass("md:p-0");
  });

  it("badges the Alerts tab with the open alert count", async () => {
    const backend = fakeBackend({
      session: exampleMe(),
      routes: { "GET /alerts/events": () => json([{ id: 1 }, { id: 2 }]) },
    });
    renderApp("/account", backend);
    const nav = await screen.findByRole("navigation", { name: "Main" });
    expect(await within(nav).findByRole("link", { name: /^Alerts\W+2 open$/ })).toBeInTheDocument();
  });
});

describe("theme", () => {
  const html = document.documentElement;

  it("follows the OS preference by default, live", async () => {
    const system = mockSystemTheme(true);
    await renderReady("/");
    expect(html).toHaveClass("dark");
    expect(html.style.colorScheme).toBe("dark");
    expect(screen.getByRole("button", { name: /^Theme: System/ })).toBeInTheDocument();

    act(() => system.set(false));
    expect(html).not.toHaveClass("dark");
    expect(html.style.colorScheme).toBe("light");
  });

  it("cycles system → light → dark → system and remembers the choice", async () => {
    const system = mockSystemTheme(true);
    await renderReady("/");
    const toggle = () => fireEvent.click(screen.getByRole("button", { name: /^Theme:/ }));

    toggle();
    expect(screen.getByRole("button", { name: /^Theme: Light/ })).toBeInTheDocument();
    expect(html).not.toHaveClass("dark");
    expect(localStorage.getItem("vantage.theme")).toBe("light");
    // A pinned theme ignores OS changes.
    act(() => system.set(true));
    expect(html).not.toHaveClass("dark");

    toggle();
    expect(screen.getByRole("button", { name: /^Theme: Dark/ })).toBeInTheDocument();
    expect(html).toHaveClass("dark");
    expect(localStorage.getItem("vantage.theme")).toBe("dark");

    toggle();
    expect(screen.getByRole("button", { name: /^Theme: System/ })).toBeInTheDocument();
    expect(localStorage.getItem("vantage.theme")).toBeNull();
    expect(html).toHaveClass("dark");
  });

  it("restores a saved choice on load", async () => {
    mockSystemTheme(false);
    localStorage.setItem("vantage.theme", "dark");
    await renderReady("/");
    expect(html).toHaveClass("dark");
    expect(screen.getByRole("button", { name: /^Theme: Dark/ })).toBeInTheDocument();
  });

  it("ignores an unknown saved value", async () => {
    localStorage.setItem("vantage.theme", "<script>");
    await renderReady("/");
    expect(screen.getByRole("button", { name: /^Theme: System/ })).toBeInTheDocument();
  });

  it("still works when storage is blocked", async () => {
    const blocked = () => {
      throw new DOMException("blocked", "SecurityError");
    };
    const spies = [
      vi.spyOn(Storage.prototype, "getItem").mockImplementation(blocked),
      vi.spyOn(Storage.prototype, "setItem").mockImplementation(blocked),
    ];
    try {
      await renderReady("/");
      fireEvent.click(screen.getByRole("button", { name: /^Theme:/ }));
      expect(screen.getByRole("button", { name: /^Theme: Light/ })).toBeInTheDocument();
    } finally {
      spies.forEach((s) => s.mockRestore());
    }
  });

  it("stores nothing but the theme preference", async () => {
    await renderReady("/");
    fireEvent.click(screen.getByRole("button", { name: /^Theme:/ }));
    expect(Object.keys(localStorage)).toEqual(["vantage.theme"]);
    expect(sessionStorage.length).toBe(0);
  });
});
