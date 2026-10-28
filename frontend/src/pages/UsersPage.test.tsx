import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import type { UserInfo } from "../api";
import { exampleMe, fakeBackend, json, mockSystemTheme, renderApp } from "../test-utils";
import type { FakeBackendOptions } from "../test-utils";

const NEW_PASSWORD = "example-new-password-456";
const LAST_ADMIN = "this is the last admin: make another user an admin first";

beforeEach(() => {
  mockSystemTheme(false);
});

function user(id: number, username: string, role: UserInfo["role"]): UserInfo {
  return {
    id,
    username,
    role,
    created_at: "2026-01-02T03:04:05+00:00",
    last_login_at: id === 1 ? "2026-01-03T03:04:05+00:00" : null,
  };
}

const ADMIN = user(1, "admin", "admin");
const VIEWER = user(2, "viewer1", "viewer");

function backendWith(routes: FakeBackendOptions["routes"] = {}, users = [ADMIN, VIEWER]) {
  return fakeBackend({
    session: exampleMe("admin"),
    routes: { "GET /users": () => json(users), ...routes },
  });
}

const noContent = () => new Response(null, { status: 204 });

/** The JSON body of the last request to `route` ("METHOD /path"). */
function sentBody(backend: ReturnType<typeof fakeBackend>, route: string): unknown {
  const call = [...backend.fetch.mock.calls]
    .reverse()
    .find(
      ([input, init]) =>
        `${init?.method ?? "GET"} ${String(input).slice("/api/v1".length)}` === route,
    );
  if (!call) throw new Error(`${route} was not requested`);
  return call[1]?.body ? JSON.parse(String(call[1].body)) : undefined;
}

const routesSent = (backend: ReturnType<typeof fakeBackend>) =>
  backend.requests().map((r) => r.route);

async function findRow(username: string) {
  const list = await screen.findByRole("list", { name: "Users" });
  const row = within(list)
    .getAllByRole("listitem")
    .find((item) => within(item).queryByText(username) !== null);
  if (!row) throw new Error(`no row for ${username}`);
  return row;
}

function fill(label: string | RegExp, value: string, within_: HTMLElement = document.body) {
  fireEvent.change(within(within_).getByLabelText(label), { target: { value } });
}

function passwordInputs(): HTMLInputElement[] {
  return [...document.querySelectorAll<HTMLInputElement>('input[type="password"]')];
}

describe("users page", () => {
  it("lists every user with their role, and marks you", async () => {
    renderApp("/users", backendWith());
    const me = await findRow("admin");
    expect(within(me).getByText("Admin")).toBeInTheDocument();
    expect(within(me).getByText("(you)")).toBeInTheDocument();
    const other = await findRow("viewer1");
    expect(within(other).getByText("Viewer")).toBeInTheDocument();
    expect(within(other).getByText(/Last login Never/)).toBeInTheDocument();
    // Password inputs start empty: nothing is ever pre-filled.
    for (const field of passwordInputs()) expect(field.value).toBe("");
  });

  it("is in the admin's menu and hidden from viewers, who never request it", async () => {
    const admin = renderApp("/", backendWith());
    const nav = await screen.findByRole("navigation", { name: "Main" });
    expect(within(nav).getByRole("link", { name: "Users" })).toBeInTheDocument();
    admin.unmount();

    const backend = fakeBackend({ session: exampleMe("viewer") });
    renderApp("/users", backend);
    const viewerNav = await screen.findByRole("navigation", { name: "Main" });
    expect(within(viewerNav).queryByRole("link", { name: "Users" })).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { level: 1, name: "Users" })).not.toBeInTheDocument();
    expect(routesSent(backend)).not.toContain("GET /users");
  });

  it("adds a user and clears the password fields", async () => {
    const backend = backendWith({
      "POST /users": (body) => json(user(3, (body as { username: string }).username, "admin"), 201),
    });
    renderApp("/users", backend);
    const form = await screen.findByRole("region", { name: "Add a user" });
    fill("Username", "ops.user", form);
    fireEvent.change(within(form).getByLabelText("Role"), { target: { value: "admin" } });
    fill("New password", NEW_PASSWORD, form);
    fill("Repeat the new password", NEW_PASSWORD, form);
    fireEvent.click(within(form).getByRole("button", { name: "Add user" }));

    expect(await within(form).findByText("Added ops.user.")).toBeInTheDocument();
    expect(sentBody(backend, "POST /users")).toEqual({
      username: "ops.user",
      password: NEW_PASSWORD,
      role: "admin",
    });
    expect(backend.requests().find((r) => r.route === "POST /users")?.csrf).toBe(
      exampleMe().csrf_token,
    );
    for (const field of passwordInputs()) expect(field.value).toBe("");
    expect(document.body.innerHTML).not.toContain(NEW_PASSWORD);
    // The list is fetched again.
    await waitFor(() =>
      expect(routesSent(backend).filter((r) => r === "GET /users")).toHaveLength(2),
    );
  });

  it("checks the new user's fields before sending anything", async () => {
    const backend = backendWith();
    renderApp("/users", backend);
    const form = await screen.findByRole("region", { name: "Add a user" });
    const submit = () => fireEvent.click(within(form).getByRole("button", { name: "Add user" }));

    fill("Username", "has space", form);
    submit();
    expect(await within(form).findByRole("alert")).toHaveTextContent("Invalid username");

    fill("Username", "ops.user", form);
    fill("New password", "too-short", form);
    fill("Repeat the new password", "too-short", form);
    submit();
    expect(await within(form).findByRole("alert")).toHaveTextContent("12 to 1024 characters");

    fill("New password", NEW_PASSWORD, form);
    fill("Repeat the new password", `${NEW_PASSWORD}x`, form);
    submit();
    expect(await within(form).findByRole("alert")).toHaveTextContent("don't match");
    expect(routesSent(backend)).not.toContain("POST /users");
  });

  it("shows a taken username as a clear error", async () => {
    const backend = backendWith({
      "POST /users": () => json({ detail: "a user with that username already exists" }, 409),
    });
    renderApp("/users", backend);
    const form = await screen.findByRole("region", { name: "Add a user" });
    fill("Username", "Admin", form);
    fill("New password", NEW_PASSWORD, form);
    fill("Repeat the new password", NEW_PASSWORD, form);
    fireEvent.click(within(form).getByRole("button", { name: "Add user" }));
    expect(await within(form).findByRole("alert")).toHaveTextContent(
      "a user with that username already exists",
    );
    for (const field of passwordInputs()) expect(field.value).toBe("");
  });

  it("changes a role", async () => {
    const backend = backendWith({ "PATCH /users/2": () => json({ ...VIEWER, role: "admin" }) });
    renderApp("/users", backend);
    const row = await findRow("viewer1");
    fireEvent.click(within(row).getByRole("button", { name: "Make viewer1 admin" }));
    expect(await within(row).findByRole("status")).toHaveTextContent("viewer1 is now admin.");
    expect(sentBody(backend, "PATCH /users/2")).toEqual({ role: "admin" });
  });

  it("explains the last-admin rule when the backend refuses a demotion", async () => {
    const backend = backendWith({ "PATCH /users/1": () => json({ detail: LAST_ADMIN }, 409) }, [
      ADMIN,
      VIEWER,
    ]);
    const { router } = renderApp("/users", backend);
    const me = await findRow("admin");
    fireEvent.click(within(me).getByRole("button", { name: "Make admin viewer" }));
    expect(await within(me).findByRole("alert")).toHaveTextContent(`Not done: ${LAST_ADMIN}.`);
    expect(router.state.location.pathname).toBe("/users");
  });

  it("explains the last-admin rule when the backend refuses a delete", async () => {
    const backend = backendWith({ "DELETE /users/2": () => json({ detail: LAST_ADMIN }, 409) }, [
      ADMIN,
      user(2, "admin2", "admin"),
    ]);
    renderApp("/users", backend);
    const row = await findRow("admin2");
    fireEvent.click(within(row).getByRole("button", { name: "Delete admin2" }));
    fireEvent.click(within(row).getByRole("button", { name: "Confirm deleting admin2" }));
    expect(await within(row).findByRole("alert")).toHaveTextContent(LAST_ADMIN);
  });

  it("leaves the page after you demote yourself", async () => {
    const backend = backendWith({ "PATCH /users/1": () => json({ ...ADMIN, role: "viewer" }) }, [
      ADMIN,
      user(3, "admin2", "admin"),
    ]);
    const { router } = renderApp("/users", backend);
    const me = await findRow("admin");
    backend.state.session = exampleMe("viewer", "admin");
    fireEvent.click(within(me).getByRole("button", { name: "Make admin viewer" }));
    await waitFor(() => expect(router.state.location.pathname).toBe("/"));
    // The session is loaded again, so the menu follows the new role.
    const nav = await screen.findByRole("navigation", { name: "Main" });
    await waitFor(() =>
      expect(within(nav).queryByRole("link", { name: "Users" })).not.toBeInTheDocument(),
    );
  });

  it("resets another user's password", async () => {
    const backend = backendWith({ "POST /users/2/password": noContent });
    renderApp("/users", backend);
    const row = await findRow("viewer1");
    fireEvent.click(within(row).getByRole("button", { name: "Reset the password of viewer1" }));
    const form = within(row).getByRole("form", { name: "Reset the password of viewer1" });
    fill("New password", NEW_PASSWORD, form);
    fill("Repeat the new password", NEW_PASSWORD, form);
    fireEvent.click(within(form).getByRole("button", { name: "Set new password" }));

    expect(await within(row).findByRole("status")).toHaveTextContent(
      "New password set for viewer1. Their sessions were logged out.",
    );
    expect(sentBody(backend, "POST /users/2/password")).toEqual({ password: NEW_PASSWORD });
    expect(within(row).queryByRole("form")).not.toBeInTheDocument();
    expect(document.body.innerHTML).not.toContain(NEW_PASSWORD);
  });

  it("doesn't send a reset whose passwords don't match", async () => {
    const backend = backendWith();
    renderApp("/users", backend);
    const row = await findRow("viewer1");
    fireEvent.click(within(row).getByRole("button", { name: "Reset the password of viewer1" }));
    const form = within(row).getByRole("form", { name: "Reset the password of viewer1" });
    fill("New password", NEW_PASSWORD, form);
    fill("Repeat the new password", "example-other-password", form);
    fireEvent.click(within(form).getByRole("button", { name: "Set new password" }));
    expect(await within(form).findByRole("alert")).toHaveTextContent("don't match");
    expect(routesSent(backend)).not.toContain("POST /users/2/password");
  });

  it("deletes a user after confirmation", async () => {
    let users = [ADMIN, VIEWER];
    const backend = fakeBackend({
      session: exampleMe("admin"),
      routes: {
        "GET /users": () => json(users),
        "DELETE /users/2": () => {
          users = [ADMIN];
          return noContent();
        },
      },
    });
    renderApp("/users", backend);
    const row = await findRow("viewer1");
    fireEvent.click(within(row).getByRole("button", { name: "Delete viewer1" }));
    expect(routesSent(backend)).not.toContain("DELETE /users/2");
    fireEvent.click(within(row).getByRole("button", { name: "Confirm deleting viewer1" }));
    await waitFor(() => expect(screen.queryByText("viewer1")).not.toBeInTheDocument());
    expect(routesSent(backend)).toContain("DELETE /users/2");
  });

  it("offers no reset or delete on your own row, only your own password form", async () => {
    renderApp("/users", backendWith());
    const me = await findRow("admin");
    expect(within(me).queryByRole("button", { name: /Reset the password/ })).toBeNull();
    expect(within(me).queryByRole("button", { name: /Delete/ })).toBeNull();
    expect(within(me).getByRole("link", { name: "Change my password" })).toHaveAttribute(
      "href",
      "/account",
    );
  });

  it("reports a failed load and retries", async () => {
    let fail = true;
    const backend = fakeBackend({
      session: exampleMe("admin"),
      routes: {
        "GET /users": () => (fail ? json({ detail: "admin role required" }, 403) : json([ADMIN])),
      },
    });
    renderApp("/users", backend);
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not load the users");
    fail = false;
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await findRow("admin")).toBeInTheDocument();
  });

  it("keeps passwords out of browser storage", async () => {
    const backend = backendWith({
      "POST /users": () => json(user(3, "ops.user", "viewer"), 201),
    });
    renderApp("/users", backend);
    const form = await screen.findByRole("region", { name: "Add a user" });
    fill("Username", "ops.user", form);
    fill("New password", NEW_PASSWORD, form);
    fill("Repeat the new password", NEW_PASSWORD, form);
    fireEvent.click(within(form).getByRole("button", { name: "Add user" }));
    await within(form).findByText("Added ops.user.");
    const stored = JSON.stringify({ ...localStorage }) + JSON.stringify({ ...sessionStorage });
    expect(stored).not.toContain(NEW_PASSWORD);
  });
});
