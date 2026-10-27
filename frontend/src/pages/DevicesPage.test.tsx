import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import type { Instance, TestResult } from "../api";
import { exampleMe, fakeBackend, json, mockSystemTheme, renderApp } from "../test-utils";
import type { FakeBackendOptions } from "../test-utils";
import { exampleInstance, FAKE_TYPE, INSTANCE_ID } from "./devices/testFixtures";

const NEW_TOKEN = "example-token-new";
const ITEM = `/instances/${INSTANCE_ID}`;

beforeEach(() => {
  mockSystemTheme(false);
});

function backendWith(
  instances: Instance[],
  routes: FakeBackendOptions["routes"] = {},
  role: "admin" | "viewer" = "admin",
) {
  return fakeBackend({
    session: exampleMe(role),
    routes: {
      "GET /integrations": () => json([FAKE_TYPE]),
      "GET /instances": () => json(instances),
      [`GET ${ITEM}`]: () => json(instances[0]),
      ...routes,
    },
  });
}

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

function fill(label: string | RegExp, value: string) {
  fireEvent.change(screen.getByLabelText(label), { target: { value } });
}

describe("device list", () => {
  it("shows each device with its type, site and status", async () => {
    renderApp(
      "/devices",
      backendWith([
        exampleInstance({
          name: "nas-1",
          status: {
            state: "down",
            checked_at: "2026-01-02T03:05:05Z",
            last_success_at: null,
            error: "ConnectTimeout",
          },
        }),
        exampleInstance({
          id: "00000000-0000-4000-8000-000000000002",
          name: "fw-1",
          site: null,
          enabled: false,
          status: { state: "unknown", checked_at: null, last_success_at: null, error: null },
        }),
      ]),
    );
    const list = await screen.findByRole("list", { name: "Devices" });
    const [nas, fw] = within(list).getAllByRole("listitem");
    if (!nas || !fw) throw new Error("expected two devices");
    expect(nas).toHaveTextContent("Fake device · lab");
    expect(nas).toHaveTextContent("Status: Down");
    expect(nas).toHaveTextContent("ConnectTimeout");
    expect(nas).toHaveTextContent("storage");
    expect(fw).toHaveTextContent("Fake device · No site");
    expect(fw).toHaveTextContent("Status: Unknown");
    expect(fw).toHaveTextContent("Disabled");
    expect(within(fw).getByRole("button", { name: "Enable fw-1" })).toBeInTheDocument();
  });

  it("says when there are no devices", async () => {
    renderApp("/devices", backendWith([]));
    expect(await screen.findByText(/No devices yet/)).toBeInTheDocument();
  });

  it("shows a load error", async () => {
    renderApp(
      "/devices",
      backendWith([], { "GET /instances": () => json({ detail: "database unavailable" }, 400) }),
    );
    expect(await screen.findByRole("alert")).toHaveTextContent("database unavailable");
  });

  it("is read-only for viewers", async () => {
    renderApp("/devices", backendWith([exampleInstance()], {}, "viewer"));
    await screen.findByRole("list", { name: "Devices" });
    expect(screen.queryByRole("link", { name: "Add device" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Delete|Disable|Test/ })).not.toBeInTheDocument();
  });

  it("keeps viewers out of the add and edit pages", async () => {
    for (const path of ["/devices/new", `/devices/${INSTANCE_ID}`]) {
      const { unmount } = renderApp(path, backendWith([exampleInstance()], {}, "viewer"));
      expect(await screen.findByRole("heading", { name: "Admins only" })).toBeInTheDocument();
      unmount();
    }
  });

  it("disables and enables a device", async () => {
    const backend = backendWith([exampleInstance()], {
      [`PATCH ${ITEM}`]: () => json(exampleInstance({ enabled: false })),
    });
    renderApp("/devices", backend);
    fireEvent.click(await screen.findByRole("button", { name: "Disable nas-1" }));
    await waitFor(() => expect(routesSent(backend)).toContain(`PATCH ${ITEM}`));
    expect(sentBody(backend, `PATCH ${ITEM}`)).toEqual({ enabled: false });
    // The list is fetched again.
    await waitFor(() =>
      expect(routesSent(backend).filter((r) => r === "GET /instances")).toHaveLength(2),
    );
  });

  it("deletes a device only after confirming", async () => {
    const backend = backendWith([exampleInstance()], {
      [`DELETE ${ITEM}`]: () => new Response(null, { status: 204 }),
    });
    renderApp("/devices", backend);
    fireEvent.click(await screen.findByRole("button", { name: "Delete nas-1" }));
    expect(routesSent(backend)).not.toContain(`DELETE ${ITEM}`);
    fireEvent.click(screen.getByRole("button", { name: "Keep" }));
    fireEvent.click(screen.getByRole("button", { name: "Delete nas-1" }));
    fireEvent.click(screen.getByRole("button", { name: "Confirm deleting nas-1" }));
    await waitFor(() => expect(routesSent(backend)).toContain(`DELETE ${ITEM}`));
  });

  it("tests a saved device and shows the firewall hint on failure", async () => {
    const failed: TestResult = {
      ok: false,
      message: "connection test timed out",
      hint: { protocol: "tcp", port: 8443, destination: "10.0.0.10", reason: "API" },
    };
    renderApp(
      "/devices",
      backendWith([exampleInstance()], { [`POST ${ITEM}/test`]: () => json(failed) }),
    );
    fireEvent.click(await screen.findByRole("button", { name: "Test nas-1" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Connection failed: connection test timed out");
    expect(alert).toHaveTextContent("Allow TCP port 8443 from the Vantage server to 10.0.0.10.");
    expect(alert).toHaveTextContent("API");
  });

  it("shows an ICMP firewall hint, which has no port", async () => {
    const failed: TestResult = {
      ok: false,
      message: "no reply from 10.0.0.30 (3 packets sent, ICMP may be blocked)",
      hint: { protocol: "icmp", port: null, destination: "10.0.0.30", reason: "ping" },
    };
    renderApp(
      "/devices",
      backendWith([exampleInstance()], { [`POST ${ITEM}/test`]: () => json(failed) }),
    );
    fireEvent.click(await screen.findByRole("button", { name: "Test nas-1" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Allow ICMP echo (ping) from the Vantage server to 10.0.0.30.");
    expect(alert).not.toHaveTextContent("port");
    expect(alert).not.toHaveTextContent("null");
  });

  it("reports a failed action", async () => {
    renderApp(
      "/devices",
      backendWith([exampleInstance()], {
        [`PATCH ${ITEM}`]: () => json({ detail: "admin role required" }, 403),
      }),
    );
    fireEvent.click(await screen.findByRole("button", { name: "Disable nas-1" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Only admins can change devices.");
  });
});

describe("add device", () => {
  async function openForm(backend = backendWith([])) {
    const result = renderApp("/devices", backend);
    fireEvent.click(await screen.findByRole("link", { name: "Add device" }));
    const types = await screen.findByRole("list", { name: "Device types" });
    fireEvent.click(within(types).getByRole("button", { name: /Fake device/ }));
    await screen.findByRole("heading", { level: 1, name: "Add Fake device" });
    return result;
  }

  it("generates the form from the schema, with empty password inputs for secrets", async () => {
    await openForm();
    expect(screen.getByLabelText("Host")).toHaveAttribute("placeholder", "192.0.2.10");
    expect(screen.getByLabelText(/^Port/)).toHaveValue("443");
    expect(screen.getByLabelText("Verify Tls")).toBeChecked();
    for (const name of ["Api Token", "Snmp Community", "Client Secret"]) {
      const secret = screen.getByLabelText(name);
      expect(secret).toHaveAttribute("type", "password");
      expect(secret).toHaveAttribute("autocomplete", "new-password");
      expect(secret).toHaveValue("");
    }
  });

  it("checks required fields before sending anything", async () => {
    const backend = backendWith([]);
    await openForm(backend);
    fireEvent.click(screen.getByRole("button", { name: "Add device" }));
    expect(screen.getByRole("alert")).toHaveTextContent("Check the highlighted fields.");
    expect(screen.getByLabelText("Name")).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByLabelText("Host")).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByLabelText("Api Token")).toHaveAccessibleDescription("Required.");
    expect(routesSent(backend)).not.toContain("POST /instances");
  });

  it("tests the unsaved config, shows the firewall hint, then saves", async () => {
    const backend = backendWith([], {
      "POST /instances/test": () =>
        json({
          ok: false,
          message: "connection test timed out",
          hint: { protocol: "tcp", port: 8443, destination: "10.0.0.10", reason: "HTTPS API" },
        }),
      "POST /instances": () => json(exampleInstance(), 201),
    });
    const { router } = await openForm(backend);
    fill("Name", "nas-1");
    fill(/^Site/, "lab");
    fill(/^Tags/, "storage, rack-2, storage");
    fill("Host", "10.0.0.10");
    fill(/^Port/, "8443");
    fill("Api Token", NEW_TOKEN);

    fireEvent.click(screen.getByRole("button", { name: "Test connection" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("connection test timed out");
    expect(alert).toHaveTextContent("Allow TCP port 8443 from the Vantage server to 10.0.0.10.");
    expect(sentBody(backend, "POST /instances/test")).toEqual({
      type_id: "fake_device",
      name: "nas-1",
      config: { host: "10.0.0.10", port: 8443, verify_tls: true, api_token: NEW_TOKEN },
    });

    fireEvent.click(screen.getByRole("button", { name: "Add device" }));
    await waitFor(() => expect(router.state.location.pathname).toBe("/devices"));
    expect(sentBody(backend, "POST /instances")).toEqual({
      type_id: "fake_device",
      name: "nas-1",
      site: "lab",
      tags: ["storage", "rack-2"],
      enabled: true,
      config: { host: "10.0.0.10", port: 8443, verify_tls: true, api_token: NEW_TOKEN },
    });
  });

  it("shows a successful test", async () => {
    await openForm(
      backendWith([], {
        "POST /instances/test": () => json({ ok: true, message: "reachable", hint: null }),
      }),
    );
    fill("Name", "nas-1");
    fill("Host", "10.0.0.10");
    fill("Api Token", NEW_TOKEN);
    fireEvent.click(screen.getByRole("button", { name: "Test connection" }));
    expect(await screen.findByText("Connection OK: reachable")).toBeInTheDocument();
  });

  it("shows the backend's validation message and keeps the form", async () => {
    await openForm(
      backendWith([], {
        "POST /instances": () =>
          json({ detail: [{ loc: ["body", "config", "host"], msg: "host is not allowed" }] }, 422),
      }),
    );
    fill("Name", "nas-1");
    fill("Host", "10.0.0.10");
    fill("Api Token", NEW_TOKEN);
    fireEvent.click(screen.getByRole("button", { name: "Add device" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("host is not allowed");
    expect(screen.getByLabelText("Host")).toHaveValue("10.0.0.10");
  });

  it("can go back to the type list", async () => {
    await openForm();
    fireEvent.click(screen.getByRole("button", { name: "Change type" }));
    expect(await screen.findByRole("list", { name: "Device types" })).toBeInTheDocument();
  });
});

describe("edit device", () => {
  async function openEdit(routes: FakeBackendOptions["routes"] = {}) {
    const backend = backendWith([exampleInstance()], routes);
    const result = renderApp(`/devices/${INSTANCE_ID}`, backend);
    await screen.findByRole("heading", { level: 1, name: "Edit nas-1" });
    return { ...result, backend };
  }

  it("fills the stored config, never the secrets, and says which secrets are set", async () => {
    await openEdit();
    expect(screen.getByLabelText("Name")).toHaveValue("nas-1");
    expect(screen.getByLabelText(/^Site/)).toHaveValue("lab");
    expect(screen.getByLabelText("Host")).toHaveValue("10.0.0.10");
    expect(screen.getByLabelText(/^Port/)).toHaveValue("8443");
    const token = screen.getByLabelText(/^Api Token/);
    expect(token).toHaveValue("");
    expect(token).toHaveAttribute("placeholder", "Unchanged");
    const badge = (input: HTMLElement) => {
      const field = input.closest("div");
      if (!field) throw new Error("field wrapper missing");
      return within(field).getByText(/^(not )?set$/).textContent;
    };
    expect(badge(token)).toBe("set");
    expect(badge(screen.getByLabelText(/^Snmp Community/))).toBe("not set");
    expect(badge(screen.getByLabelText(/^Client Secret/))).toBe("set");
  });

  it("leaves blank secrets out, so they stay unchanged", async () => {
    const { backend, router } = await openEdit({
      [`PATCH ${ITEM}`]: () => json(exampleInstance()),
    });
    fill(/^Port/, "9443");
    fill(/^Site/, "");
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(router.state.location.pathname).toBe("/devices"));
    expect(sentBody(backend, `PATCH ${ITEM}`)).toEqual({
      name: "nas-1",
      site: null,
      tags: ["storage"],
      enabled: true,
      config: { host: "10.0.0.10", port: 9443, verify_tls: true },
    });
  });

  it("sends a re-entered secret", async () => {
    const { backend } = await openEdit({ [`PATCH ${ITEM}`]: () => json(exampleInstance()) });
    fill(/^Api Token/, NEW_TOKEN);
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(routesSent(backend)).toContain(`PATCH ${ITEM}`));
    expect(sentBody(backend, `PATCH ${ITEM}`)).toMatchObject({
      config: { api_token: NEW_TOKEN },
    });
  });

  it("tests the saved config when nothing changed", async () => {
    const { backend } = await openEdit({
      [`POST ${ITEM}/test`]: () => json({ ok: true, message: "reachable", hint: null }),
    });
    fireEvent.click(screen.getByRole("button", { name: "Test connection" }));
    expect(await screen.findByText("Connection OK: reachable")).toBeInTheDocument();
    expect(routesSent(backend)).not.toContain("POST /instances/test");
  });

  it("asks for the stored secrets again before testing changed settings", async () => {
    const { backend } = await openEdit({
      "POST /instances/test": () => json({ ok: true, message: "reachable", hint: null }),
    });
    fill(/^Port/, "9443");
    fireEvent.click(screen.getByRole("button", { name: "Test connection" }));
    expect(screen.getByRole("status")).toHaveTextContent(/enter Api Token, Client Secret again/);
    expect(routesSent(backend)).not.toContain("POST /instances/test");

    fill(/^Api Token/, NEW_TOKEN);
    fill(/^Client Secret/, "example-client-secret");
    fireEvent.click(screen.getByRole("button", { name: "Test connection" }));
    expect(await screen.findByText("Connection OK: reachable")).toBeInTheDocument();
    expect(sentBody(backend, "POST /instances/test")).toMatchObject({
      type_id: "fake_device",
      config: { port: 9443, api_token: NEW_TOKEN, client_secret: "example-client-secret" },
    });
  });

  it("can remove an optional stored secret", async () => {
    const backend = backendWith(
      [
        exampleInstance({
          secrets_set: { api_token: true, client_secret: true, snmp_community: true },
        }),
      ],
      { [`PATCH ${ITEM}`]: () => json(exampleInstance()) },
    );
    renderApp(`/devices/${INSTANCE_ID}`, backend);
    fireEvent.click(await screen.findByLabelText("Remove the stored value"));
    expect(screen.getByLabelText(/^Snmp Community/)).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(routesSent(backend)).toContain(`PATCH ${ITEM}`));
    expect(sentBody(backend, `PATCH ${ITEM}`)).toMatchObject({ config: { snmp_community: null } });
  });

  it("reports an unknown device", async () => {
    renderApp(
      `/devices/${INSTANCE_ID}`,
      backendWith([], { [`GET ${ITEM}`]: () => json({ detail: "instance not found" }, 404) }),
    );
    expect(await screen.findByRole("alert")).toHaveTextContent("instance not found");
  });
});
