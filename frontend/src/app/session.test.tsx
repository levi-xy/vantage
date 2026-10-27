import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { ApiError } from "../api";
import {
  EXAMPLE_CSRF,
  EXAMPLE_PASSWORD,
  exampleMe,
  fakeBackend,
  json,
  mockSystemTheme,
  renderApp,
} from "../test-utils";
import { returnPath } from "./returnPath";
import { SESSION_ENDED, SETUP_ALREADY_DONE } from "./session";

beforeEach(() => {
  mockSystemTheme(false);
});

function fill(label: string | RegExp, value: string) {
  fireEvent.change(screen.getByLabelText(label), { target: { value } });
}

async function logIn(username: string, password = EXAMPLE_PASSWORD) {
  await screen.findByRole("heading", { level: 1, name: "Log in" });
  fill("Username", username);
  fill("Password", password);
  fireEvent.click(screen.getByRole("button", { name: "Log in" }));
}

const mainNav = () => screen.findByRole("navigation", { name: "Main" });

describe("app start", () => {
  it("asks GET /me before anything else and restores the CSRF token for writes", async () => {
    const { backend, client } = renderApp("/devices");
    expect(await screen.findByRole("heading", { level: 1, name: "Devices" })).toBeInTheDocument();
    expect(backend.requests()[0]?.route).toBe("GET /me");
    // After a reload the token exists only because /me ran: writes carry it.
    await client.request("POST", "/instances", {});
    expect(backend.requests().at(-1)).toEqual({ route: "POST /instances", csrf: EXAMPLE_CSRF });
  });

  it("shows a loading state until the session is known", async () => {
    renderApp("/");
    expect(screen.getByRole("status")).toHaveTextContent("Loading");
    expect(screen.queryByRole("navigation", { name: "Main" })).not.toBeInTheDocument();
    await mainNav();
  });

  it("sends a visitor without a session to the login page", async () => {
    const { router, backend } = renderApp("/devices", fakeBackend());
    expect(await screen.findByRole("heading", { level: 1, name: "Log in" })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/login");
    expect(backend.requests().map((r) => r.route)).toEqual(["GET /me", "GET /setup/status"]);
  });

  it("offers a retry when the backend can't be reached", async () => {
    const backend = fakeBackend({ session: exampleMe() });
    backend.fetch.mockRejectedValueOnce(new TypeError("example failure"));
    renderApp("/", backend);
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Could not reach the Vantage backend.",
    );
    expect(screen.queryByText(/example failure/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await mainNav()).toBeInTheDocument();
  });

  it("reports a backend error at startup without guessing a state", async () => {
    renderApp("/", fakeBackend({ routes: { "GET /me": () => json({ detail: "boom" }, 500) } }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The Vantage backend answered with an error.",
    );
  });
});

describe("first-run setup", () => {
  it("redirects every page to the wizard while setup is required", async () => {
    for (const path of ["/", "/login", "/audit"]) {
      const { router, unmount } = renderApp(path, fakeBackend({ setupRequired: true }));
      expect(
        await screen.findByRole("heading", { level: 1, name: "Welcome to Vantage" }),
      ).toBeInTheDocument();
      expect(router.state.location.pathname).toBe("/setup");
      unmount();
    }
  });

  it("checks the fields before sending anything", async () => {
    const { backend } = renderApp("/setup", fakeBackend({ setupRequired: true }));
    await screen.findByRole("heading", { level: 1, name: "Welcome to Vantage" });
    fill("Admin username", "-bad name");
    fill(/^Password/, "short");
    fill("Confirm password", "other");
    fireEvent.click(screen.getByRole("button", { name: "Create admin account" }));

    expect(screen.getByLabelText("Admin username")).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByText(/starting with a letter or digit/)).toBeInTheDocument();
    expect(screen.getByLabelText(/^Password/)).toHaveAccessibleDescription(
      /At least 12 characters\..*At least 12 characters\./,
    );

    fill(/^Password/, "a-long-example-password");
    fireEvent.click(screen.getByRole("button", { name: "Create admin account" }));
    expect(screen.getByText("The passwords don't match.")).toBeInTheDocument();
    expect(backend.requests().map((r) => r.route)).not.toContain("POST /setup");
  });

  it("creates the admin, logs them in and opens the dashboard", async () => {
    const { router, backend, client } = renderApp("/", fakeBackend({ setupRequired: true }));
    await screen.findByRole("heading", { level: 1, name: "Welcome to Vantage" });
    fill("Admin username", "example-admin");
    fill(/^Password/, "a-long-example-password");
    fill("Confirm password", "a-long-example-password");
    fireEvent.click(screen.getByRole("button", { name: "Create admin account" }));

    const nav = await mainNav();
    expect(router.state.location.pathname).toBe("/");
    expect(within(nav).getByRole("link", { name: "Audit log" })).toBeInTheDocument();
    expect(screen.getByText("example-admin")).toBeInTheDocument();
    expect(client.hasSession).toBe(true);
    const sent = backend.fetch.mock.calls.find(([, init]) => init?.method === "POST");
    expect(JSON.parse(String(sent?.[1]?.body))).toEqual({
      username: "example-admin",
      password: "a-long-example-password",
    });
  });

  it("shows the backend's validation message", async () => {
    renderApp(
      "/setup",
      fakeBackend({
        setupRequired: true,
        routes: {
          "POST /setup": () =>
            json({ detail: [{ loc: ["body", "password"], msg: "String too short" }] }, 422),
        },
      }),
    );
    await screen.findByRole("heading", { level: 1, name: "Welcome to Vantage" });
    fill("Admin username", "admin");
    fill(/^Password/, "a-long-example-password");
    fill("Confirm password", "a-long-example-password");
    fireEvent.click(screen.getByRole("button", { name: "Create admin account" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("String too short");
  });

  it("goes to the login page when someone else completed setup first", async () => {
    const backend = fakeBackend({ setupRequired: true });
    const { router } = renderApp("/setup", backend);
    await screen.findByRole("heading", { level: 1, name: "Welcome to Vantage" });
    backend.state.setupRequired = false;
    fill("Admin username", "admin");
    fill(/^Password/, "a-long-example-password");
    fill("Confirm password", "a-long-example-password");
    fireEvent.click(screen.getByRole("button", { name: "Create admin account" }));

    expect(await screen.findByRole("heading", { level: 1, name: "Log in" })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/login");
    expect(screen.getByRole("status")).toHaveTextContent(SETUP_ALREADY_DONE);
  });

  it("is closed once setup is done", async () => {
    const { router } = renderApp("/setup", fakeBackend());
    expect(await screen.findByRole("heading", { level: 1, name: "Log in" })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/login");
  });
});

describe("login", () => {
  it("logs in and returns to the page the user asked for", async () => {
    const { router, client, backend } = renderApp("/devices?x=1", fakeBackend());
    await logIn("admin");
    expect(await screen.findByRole("heading", { level: 1, name: "Devices" })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/devices");
    expect(router.state.location.search).toBe("?x=1");
    // The token from the login response is used for writes.
    await client.request("DELETE", "/instances/example");
    expect(backend.requests().at(-1)?.csrf).toBe(EXAMPLE_CSRF);
  });

  it("redirects a logged-in user away from the login page", async () => {
    const { router } = renderApp("/login");
    await mainNav();
    expect(router.state.location.pathname).toBe("/");
  });

  it("reports wrong credentials, clears the password and stays put without looping", async () => {
    const { router, backend, client } = renderApp("/", fakeBackend());
    await logIn("admin", "wrong-example-password");

    expect(await screen.findByRole("alert")).toHaveTextContent("Wrong username or password.");
    expect(router.state.location.pathname).toBe("/login");
    expect(screen.getByLabelText("Password")).toHaveValue("");
    expect(screen.getByLabelText("Username")).toHaveValue("admin");
    expect(client.hasSession).toBe(false);
    // Exactly one login request: the 401 didn't trigger another redirect or session check.
    await act(() => new Promise((r) => setTimeout(r, 20)));
    expect(backend.requests().map((r) => r.route)).toEqual([
      "GET /me",
      "GET /setup/status",
      "POST /auth/login",
    ]);
  });

  it("explains the rate limit", async () => {
    renderApp(
      "/",
      fakeBackend({
        routes: {
          "POST /auth/login": () =>
            json({ detail: "too many failed logins, try again later" }, 429),
        },
      }),
    );
    await logIn("admin");
    expect(await screen.findByRole("alert")).toHaveTextContent("Too many failed logins.");
  });

  it("asks for both fields before sending", async () => {
    const { backend } = renderApp("/login", fakeBackend());
    await logIn("", "");
    expect(screen.getByRole("alert")).toHaveTextContent("Enter your username and password.");
    expect(backend.requests().map((r) => r.route)).not.toContain("POST /auth/login");
  });

  it("never puts the password, token or user in web storage", async () => {
    renderApp("/", fakeBackend());
    await logIn("admin");
    await mainNav();
    expect(Object.keys(localStorage).filter((k) => k !== "vantage.theme")).toEqual([]);
    expect(sessionStorage.length).toBe(0);
    const stored = Object.values(localStorage).join(" ");
    expect(stored).not.toContain(EXAMPLE_PASSWORD);
    expect(stored).not.toContain(EXAMPLE_CSRF);
  });
});

describe("logout", () => {
  it("ends the session with the CSRF token, clears cached data and shows the login page", async () => {
    const { router, backend, client, queryClient } = renderApp("/devices");
    await mainNav();
    queryClient.setQueryData(["instances"], [{ id: "example" }]);
    fireEvent.click(screen.getByRole("button", { name: "Log out" }));

    expect(await screen.findByRole("heading", { level: 1, name: "Log in" })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/login");
    expect(backend.requests().at(-1)).toEqual({ route: "POST /auth/logout", csrf: EXAMPLE_CSRF });
    expect(queryClient.getQueryData(["instances"])).toBeUndefined();
    expect(client.hasSession).toBe(false);
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("stays logged in, with a working token, when the backend can't log out", async () => {
    const { backend, client } = renderApp(
      "/",
      fakeBackend({
        session: exampleMe(),
        routes: { "POST /auth/logout": () => json({ detail: "server error" }, 500) },
      }),
    );
    await mainNav();
    fireEvent.click(screen.getByRole("button", { name: "Log out" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Logout failed");
    expect(screen.getByRole("navigation", { name: "Main" })).toBeInTheDocument();
    await waitFor(() => expect(client.hasSession).toBe(true));
    expect(backend.requests().at(-1)?.route).toBe("GET /me");
  });

  it("shows the unreachable screen when neither logout nor the session check get through", async () => {
    const backend = fakeBackend({ session: exampleMe() });
    const { client } = renderApp("/", backend);
    await mainNav();
    backend.fetch.mockRejectedValueOnce(new TypeError("example failure"));
    backend.fetch.mockRejectedValueOnce(new TypeError("example failure"));
    fireEvent.click(screen.getByRole("button", { name: "Log out" }));

    expect(await screen.findByRole("heading", { name: "Can't load Vantage" })).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("Could not reach the Vantage backend.");
    expect(client.hasSession).toBe(false);
    // The retry asks GET /me again: the cookie was still valid, so the token is back.
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await mainNav()).toBeInTheDocument();
    expect(client.hasSession).toBe(true);
  });

  it("doesn't send the next user back to the previous user's page", async () => {
    const { router } = renderApp("/devices?x=1");
    await mainNav();
    fireEvent.click(screen.getByRole("button", { name: "Log out" }));
    await logIn("viewer");
    await mainNav();
    expect(router.state.location.pathname).toBe("/");
    expect(router.state.location.search).toBe("");
  });

  it("doesn't let cached audit entries survive into the next user's session", async () => {
    const { queryClient } = renderApp("/");
    await mainNav();
    queryClient.setQueryData(["audit"], [{ id: 1, action: "login", target_type: "user" }]);
    fireEvent.click(screen.getByRole("button", { name: "Log out" }));
    await logIn("viewer");
    await mainNav();
    expect(queryClient.getQueryData(["audit"])).toBeUndefined();
  });

  it("treats a 401 on logout as already logged out", async () => {
    const backend = fakeBackend({ session: exampleMe() });
    const { router } = renderApp("/", backend);
    await mainNav();
    backend.state.session = null;
    fireEvent.click(screen.getByRole("button", { name: "Log out" }));
    expect(await screen.findByRole("heading", { level: 1, name: "Log in" })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/login");
  });
});

describe("session expiry", () => {
  it("returns to the login page with a notice, then back to the same page", async () => {
    const backend = fakeBackend({ session: exampleMe() });
    const { router, client, queryClient } = renderApp("/devices", backend);
    await mainNav();
    queryClient.setQueryData(["instances"], [{ id: "example" }]);

    backend.state.session = null;
    await act(async () => {
      await expect(client.integrations()).rejects.toBeInstanceOf(ApiError);
    });

    expect(await screen.findByRole("heading", { level: 1, name: "Log in" })).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent(SESSION_ENDED);
    expect(queryClient.getQueryData(["instances"])).toBeUndefined();

    await logIn("admin");
    expect(await screen.findByRole("heading", { level: 1, name: "Devices" })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/devices");
  });
});

describe("role-aware navigation", () => {
  it("shows the audit log to admins", async () => {
    renderApp("/");
    const nav = await mainNav();
    expect(within(nav).getByRole("link", { name: "Audit log" })).toBeInTheDocument();
    expect(screen.getByText("(Admin)")).toBeInTheDocument();
  });

  it("hides admin-only links from viewers", async () => {
    renderApp("/", fakeBackend({ session: exampleMe("viewer") }));
    const nav = await mainNav();
    expect(within(nav).queryByRole("link", { name: "Audit log" })).not.toBeInTheDocument();
    expect(within(nav).getByRole("link", { name: "Dashboard" })).toBeInTheDocument();
    expect(within(nav).getByRole("link", { name: "Devices" })).toBeInTheDocument();
    expect(screen.getByText("(Viewer)")).toBeInTheDocument();
  });

  it("doesn't render an admin page for a viewer who types its address", async () => {
    renderApp("/audit", fakeBackend({ session: exampleMe("viewer") }));
    expect(
      await screen.findByRole("heading", { level: 1, name: "Admins only" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Audit log" })).not.toBeInTheDocument();
  });

  it("switches the menu when a viewer logs in after an admin", async () => {
    renderApp("/");
    const adminNav = await mainNav();
    expect(within(adminNav).getByRole("link", { name: "Audit log" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Log out" }));
    await logIn("viewer");
    const viewerNav = await mainNav();
    expect(within(viewerNav).queryByRole("link", { name: "Audit log" })).not.toBeInTheDocument();
  });
});

describe("returnPath", () => {
  it.each([
    [{ from: "/devices?x=1#y" }, "/devices?x=1#y"],
    [{ from: "//203.0.113.10/x" }, "/"],
    [{ from: "/\\203.0.113.10" }, "/"],
    [{ from: "https://203.0.113.10/" }, "/"],
    [{ from: "/login" }, "/"],
    [{ from: "/setup?x" }, "/"],
    [{ from: 42 }, "/"],
    [null, "/"],
    ["/devices", "/"],
  ])("%j → %s", (state, expected) => {
    expect(returnPath(state)).toBe(expected);
  });
});
