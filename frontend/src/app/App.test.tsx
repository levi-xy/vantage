import { act, fireEvent, screen, within } from "@testing-library/react";
import { mockSystemTheme, renderApp } from "../test-utils";

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
    ["/audit", "Audit log"],
    ["/users", "Users"],
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
  it("collapses the menu on phones and closes it after navigating", async () => {
    await renderReady("/");
    const nav = screen.getByRole("navigation", { name: "Main" });
    const button = screen.getByRole("button", { name: "Open menu" });
    // Hidden below md, always shown from md up.
    expect(nav).toHaveClass("hidden", "md:block");
    expect(button).toHaveAttribute("aria-expanded", "false");
    expect(button).toHaveClass("md:hidden");

    fireEvent.click(button);
    expect(nav).toHaveClass("block");
    expect(nav).not.toHaveClass("hidden");
    expect(screen.getByRole("button", { name: "Close menu" })).toHaveAttribute(
      "aria-expanded",
      "true",
    );

    fireEvent.click(within(nav).getByRole("link", { name: "Audit log" }));
    expect(await screen.findByRole("heading", { level: 1, name: "Audit log" })).toBeInTheDocument();
    expect(nav).toHaveClass("hidden");
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
