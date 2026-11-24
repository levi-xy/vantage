import { fireEvent, screen, waitFor } from "@testing-library/react";
import { exampleMe, fakeBackend, json, mockSystemTheme, renderApp } from "../test-utils";

beforeEach(() => {
  mockSystemTheme(false);
});

const FAKE_SECRET = ["example", "pass", "word"].join("-");

function promBackend(
  url = "http://192.0.2.10:9090",
  probe: { ok: boolean; reason: string | null } = { ok: true, reason: null },
) {
  const state = { kind: "none", username: null as string | null, set: false };
  const view = () => ({
    url,
    mode: "external",
    auth: { kind: state.kind, username: state.username, secret_set: state.set },
  });
  return fakeBackend({
    session: exampleMe("admin"),
    routes: {
      "GET /settings/prometheus": () => json(view()),
      "PUT /settings/prometheus": (body) => {
        const { auth } = body as { auth: { kind: string; username?: string } };
        state.kind = auth.kind;
        state.username = auth.username ?? null;
        state.set = auth.kind !== "none";
        return json(view());
      },
      "POST /settings/prometheus/test": () => json(probe),
    },
  });
}

describe("settings: prometheus", () => {
  it("shows the address and saves basic auth without ever showing the password", async () => {
    const backend = promBackend();
    renderApp("/settings/prometheus", backend);
    expect(await screen.findByTestId("prom-url")).toHaveTextContent("http://192.0.2.10:9090");
    expect(screen.getByText("external")).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Authentication"), { target: { value: "basic" } });
    expect(screen.getByText(/plain http:\/\//)).toHaveTextContent(
      "the password travels unencrypted",
    );
    expect(screen.getByTestId("secret-state")).toHaveTextContent("not set");
    fireEvent.change(screen.getByLabelText("User name"), { target: { value: "example-user" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: FAKE_SECRET } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => {
      const put = backend.fetch.mock.calls.find(([, init]) => init?.method === "PUT");
      expect(JSON.parse(String(put?.[1]?.body))).toEqual({
        auth: { kind: "basic", username: "example-user", secret: FAKE_SECRET },
      });
    });
    await waitFor(() => expect(screen.getByTestId("secret-state")).toHaveTextContent("set"));
    expect((screen.getByLabelText("Password") as HTMLInputElement).value).toBe("");
    expect(document.body.innerHTML).not.toContain(FAKE_SECRET);
  });

  it("warns about plain http:// for a bearer token too, and not over https://", async () => {
    renderApp("/settings/prometheus", promBackend());
    await screen.findByTestId("prom-url");
    expect(screen.queryByText(/plain http:\/\//)).not.toBeInTheDocument(); // no auth yet
    fireEvent.change(screen.getByLabelText("Authentication"), { target: { value: "bearer" } });
    expect(screen.getByText(/plain http:\/\//)).toHaveTextContent("the token travels unencrypted");
  });

  it("shows no plain-http warning for an https:// address", async () => {
    renderApp("/settings/prometheus", promBackend("https://192.0.2.10:9090"));
    await screen.findByTestId("prom-url");
    for (const kind of ["basic", "bearer"]) {
      fireEvent.change(screen.getByLabelText("Authentication"), { target: { value: kind } });
      expect(screen.queryByText(/plain http:\/\//)).not.toBeInTheDocument();
    }
  });

  it("tests the saved settings and explains a refusal", async () => {
    renderApp(
      "/settings/prometheus",
      promBackend("https://192.0.2.10:9090", { ok: false, reason: "auth_failed" }),
    );
    fireEvent.click(await screen.findByRole("button", { name: "Test" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/Settings → Prometheus/);
  });

  it("says when the stored credentials can't be read", async () => {
    const backend = fakeBackend({
      session: exampleMe("admin"),
      routes: {
        "GET /settings/prometheus": () =>
          json({
            url: "https://192.0.2.10:9090",
            mode: "external",
            auth: { kind: "basic", username: "example-user", secret_set: true, unreadable: true },
          }),
      },
    });
    renderApp("/settings/prometheus", backend);
    expect(await screen.findByRole("alert")).toHaveTextContent(/can't be read/);
  });

  it("is not in a viewer's settings", async () => {
    const backend = fakeBackend({ session: exampleMe("viewer") });
    renderApp("/settings/prometheus", backend);
    await screen.findByRole("heading", { level: 1 });
    expect(screen.queryByLabelText("Authentication")).not.toBeInTheDocument();
    expect(backend.fetch.mock.calls.some(([u]) => String(u).includes("/settings/prometheus"))).toBe(
      false,
    );
  });
});
