import { fireEvent, screen, within } from "@testing-library/react";
import {
  EXAMPLE_PASSWORD,
  exampleMe,
  fakeBackend,
  json,
  mockSystemTheme,
  renderApp,
} from "../test-utils";
import type { FakeBackendOptions } from "../test-utils";

const NEW_PASSWORD = "example-new-password-456";

beforeEach(() => {
  mockSystemTheme(false);
});

function backendWith(
  routes: FakeBackendOptions["routes"] = {},
  role: "admin" | "viewer" = "viewer",
) {
  return fakeBackend({ session: exampleMe(role), routes });
}

function passwordInputs(): HTMLInputElement[] {
  return [...document.querySelectorAll<HTMLInputElement>('input[type="password"]')];
}

async function submitChange(current: string, next: string, repeated = next) {
  const form = await screen.findByRole("region", { name: "Change my password" });
  fireEvent.change(within(form).getByLabelText("Current password"), { target: { value: current } });
  fireEvent.change(within(form).getByLabelText("New password"), { target: { value: next } });
  fireEvent.change(within(form).getByLabelText("Repeat the new password"), {
    target: { value: repeated },
  });
  fireEvent.click(within(form).getByRole("button", { name: "Change password" }));
  return form;
}

const routesSent = (backend: ReturnType<typeof fakeBackend>) =>
  backend.requests().map((r) => r.route);

describe("my account", () => {
  it("is in every user's menu", async () => {
    renderApp("/", backendWith());
    const nav = await screen.findByRole("navigation", { name: "Main" });
    expect(within(nav).getByRole("link", { name: "My account" })).toHaveAttribute(
      "href",
      "/account",
    );
  });

  it("changes your password and clears the fields", async () => {
    let sent: unknown;
    const backend = backendWith({
      "POST /me/password": (body) => {
        sent = body;
        return new Response(null, { status: 204 });
      },
    });
    renderApp("/account", backend);
    for (const field of passwordInputs()) expect(field.value).toBe("");
    const form = await submitChange(EXAMPLE_PASSWORD, NEW_PASSWORD);

    expect(await within(form).findByRole("status")).toHaveTextContent(
      "Password changed. Your other sessions were logged out.",
    );
    expect(sent).toEqual({ current_password: EXAMPLE_PASSWORD, new_password: NEW_PASSWORD });
    expect(backend.requests().find((r) => r.route === "POST /me/password")?.csrf).toBe(
      exampleMe().csrf_token,
    );
    for (const field of passwordInputs()) expect(field.value).toBe("");
    expect(document.body.innerHTML).not.toContain(NEW_PASSWORD);
    const stored = JSON.stringify({ ...localStorage }) + JSON.stringify({ ...sessionStorage });
    expect(stored).not.toContain(NEW_PASSWORD);
  });

  it("says when the current password is wrong, and stays logged in", async () => {
    const backend = backendWith({
      "POST /me/password": () => json({ detail: "the current password is wrong" }, 400),
    });
    const { router } = renderApp("/account", backend);
    const form = await submitChange("example-wrong-password", NEW_PASSWORD);
    expect(await within(form).findByRole("alert")).toHaveTextContent(
      "The current password is wrong.",
    );
    expect(router.state.location.pathname).toBe("/account");
    for (const field of passwordInputs()) expect(field.value).toBe("");
  });

  it("says when too many wrong passwords locked the account", async () => {
    renderApp(
      "/account",
      backendWith({
        "POST /me/password": () => json({ detail: "too many failed logins, try again later" }, 429),
      }),
    );
    const form = await submitChange("example-wrong-password", NEW_PASSWORD);
    expect(await within(form).findByRole("alert")).toHaveTextContent("Too many wrong passwords");
  });

  it("checks the new password before sending anything", async () => {
    const backend = backendWith();
    renderApp("/account", backend);
    let form = await submitChange("", NEW_PASSWORD);
    expect(await within(form).findByRole("alert")).toHaveTextContent("current password");
    form = await submitChange(EXAMPLE_PASSWORD, "too-short");
    expect(await within(form).findByRole("alert")).toHaveTextContent("12 to 1024 characters");
    form = await submitChange(EXAMPLE_PASSWORD, NEW_PASSWORD, `${NEW_PASSWORD}x`);
    expect(await within(form).findByRole("alert")).toHaveTextContent("don't match");
    expect(routesSent(backend)).not.toContain("POST /me/password");
  });
});
