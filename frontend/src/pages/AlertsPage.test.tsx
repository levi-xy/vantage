import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import type { Channel } from "../api";
import { exampleMe, fakeBackend, json, mockSystemTheme, renderApp } from "../test-utils";
import type { FakeBackendOptions } from "../test-utils";

const TOKEN = "123456789:example-token-telegram-00000000";

beforeEach(() => {
  mockSystemTheme(false);
});

const OPS: Channel = {
  id: 1,
  type: "telegram",
  name: "ops",
  config: { chat_id: "-1001234567890" },
  secrets_set: { bot_token: true },
};

function backendWith(routes: FakeBackendOptions["routes"] = {}, channels = [OPS]) {
  return fakeBackend({
    session: exampleMe("admin"),
    routes: { "GET /channels": () => json(channels), ...routes },
  });
}

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

function fill(name: string, value: string, scope: HTMLElement) {
  fireEvent.change(within(scope).getByLabelText(name), { target: { value } });
}

describe("alerts page: channels", () => {
  it("lists channels with set/not set and creates one with a password token field", async () => {
    const backend = backendWith({ "POST /channels": () => json(OPS, 201) });
    renderApp("/alerts", backend);
    const list = await screen.findByRole("list", { name: "Channels" });
    expect(within(list).getByText(/bot token set/)).toBeInTheDocument();

    const form = screen.getByRole("form", { name: "Add a Telegram channel" });
    const tokenInput = within(form).getByLabelText<HTMLInputElement>("Bot token");
    expect(tokenInput.type).toBe("password");
    expect(tokenInput.value).toBe("");
    fill("Name", "ops", form);
    fill("Chat ID", "-1001234567890", form);
    fill("Bot token", TOKEN, form);
    fireEvent.click(within(form).getByRole("button", { name: "Add channel" }));

    await waitFor(() => expect(tokenInput.value).toBe(""));
    expect(sentBody(backend, "POST /channels")).toEqual({
      type: "telegram",
      name: "ops",
      config: { chat_id: "-1001234567890", bot_token: TOKEN },
    });
  });

  it("edits without re-sending the token when the field is left empty", async () => {
    const backend = backendWith({ "PATCH /channels/1": () => json(OPS) });
    renderApp("/alerts", backend);
    fireEvent.click(await screen.findByRole("button", { name: "Edit ops" }));
    const form = screen.getByRole("form", { name: "Edit ops" });
    const tokenInput = within(form).getByLabelText<HTMLInputElement>("Bot token");
    expect(tokenInput.value).toBe("");
    expect(within(form).getByText(/^Set\./)).toBeInTheDocument();
    fill("Chat ID", "@vantage_ops", form);
    fireEvent.click(within(form).getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(sentBody(backend, "PATCH /channels/1")).toEqual({
        name: "ops",
        config: { chat_id: "@vantage_ops" },
      }),
    );
  });

  it("sends a test message and shows a failure", async () => {
    const backend = backendWith({
      "POST /channels/1/test": () =>
        json({ ok: false, message: "Telegram API answered HTTP 401: Unauthorized" }),
    });
    renderApp("/alerts", backend);
    fireEvent.click(await screen.findByRole("button", { name: "Send a test message to ops" }));
    expect(await screen.findByText(/Test failed: Telegram API answered HTTP 401/)).toBeVisible();
  });
});
