import { fireEvent, screen } from "@testing-library/react";
import { EXAMPLE_SETUP_CODE, fakeBackend, mockSystemTheme, renderApp } from "../test-utils";

beforeEach(() => {
  mockSystemTheme(false);
});

function fill(label: string | RegExp, value: string) {
  fireEvent.change(screen.getByLabelText(label), { target: { value } });
}

async function fillForm(code: string) {
  await screen.findByRole("heading", { level: 1, name: "Welcome to Vantage" });
  fill(/^Setup code/, code);
  fill("Admin username", "example-admin");
  fill(/^Password/, "a-long-example-password");
  fill("Confirm password", "a-long-example-password");
  fireEvent.click(screen.getByLabelText("I have backed up my master key"));
  fireEvent.click(screen.getByRole("button", { name: "Create admin account" }));
}

describe("setup code", () => {
  it("is a required one-time-code field with a hint on getting a new one", async () => {
    const { backend } = renderApp("/setup", fakeBackend({ setupRequired: true }));
    await fillForm("");
    const field = screen.getByLabelText(/^Setup code/);
    expect(field).toHaveAttribute("autocomplete", "one-time-code");
    expect(field).toHaveAttribute("aria-invalid", "true");
    expect(field).toHaveAccessibleDescription(/Printed by setup\.sh\..*app\.cli setup-code/);
    expect(backend.requests().map((r) => r.route)).not.toContain("POST /setup");
  });

  it("explains a wrong or used code", async () => {
    renderApp("/setup", fakeBackend({ setupRequired: true }));
    await fillForm("WRONG-CODE");
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "That setup code is wrong or was already used.",
    );
    expect(screen.getByLabelText(/^Setup code/)).toHaveValue("WRONG-CODE");
  });

  it("sends the code with the new admin", async () => {
    const { backend } = renderApp("/setup", fakeBackend({ setupRequired: true }));
    await fillForm(EXAMPLE_SETUP_CODE);
    await screen.findByRole("navigation", { name: "Main" });
    const sent = backend.fetch.mock.calls.find(([, init]) => init?.method === "POST");
    expect(JSON.parse(String(sent?.[1]?.body))).toMatchObject({ setup_code: EXAMPLE_SETUP_CODE });
  });
});

describe("master key reminder", () => {
  it("is shown, and the button stays disabled until the box is ticked", async () => {
    renderApp("/setup", fakeBackend({ setupRequired: true }));
    await screen.findByRole("heading", { level: 1, name: "Welcome to Vantage" });
    expect(screen.getByRole("heading", { name: "Back up your master key" })).toBeInTheDocument();
    const button = screen.getByRole("button", { name: "Create admin account" });
    const box = screen.getByLabelText("I have backed up my master key");
    expect(button).toBeDisabled();
    fireEvent.click(box);
    expect(button).toBeEnabled();
    fireEvent.click(box);
    expect(button).toBeDisabled();
  });

  it("is not sent to the backend", async () => {
    const { backend } = renderApp("/setup", fakeBackend({ setupRequired: true }));
    await fillForm(EXAMPLE_SETUP_CODE);
    await screen.findByRole("navigation", { name: "Main" });
    const sent = backend.fetch.mock.calls.find(([, init]) => init?.method === "POST");
    expect(Object.keys(JSON.parse(String(sent?.[1]?.body))).sort()).toEqual([
      "password",
      "setup_code",
      "username",
    ]);
  });
});
