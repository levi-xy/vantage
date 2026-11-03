import { act, screen } from "@testing-library/react";
import {
  exampleMe,
  fakeBackend,
  FakeWebSocket,
  json,
  mockSystemTheme,
  renderApp,
} from "../test-utils";
import type { Instance } from "../api";
import { QueryClient } from "@tanstack/react-query";
import { applyMessage, LIVE_RATE_KEY } from "./live";

const nas = {
  id: "i-1",
  type_id: "ping",
  name: "nas-1",
  site: null,
  tags: [],
  enabled: true,
  config: {},
  secrets_set: {},
  status: { state: "pending", checked_at: null, last_success_at: null, error: null },
  created_at: "2026-10-29T00:00:00Z",
  updated_at: "2026-10-29T00:00:00Z",
} satisfies Instance;

beforeEach(() => {
  mockSystemTheme(false);
});

function backend() {
  return fakeBackend({
    session: exampleMe(),
    routes: { "GET /instances": () => json([nas]) },
  });
}

function sock(n: number): FakeWebSocket {
  const s = FakeWebSocket.instances[n];
  if (!s) throw new Error(`no socket ${n}`);
  return s;
}

const listFetches = (b: ReturnType<typeof backend>) =>
  b.requests().filter((r) => r.route === "GET /instances").length;

describe("live updates", () => {
  it("shows live, applies status pushes and refetches on refresh hints", async () => {
    const b = backend();
    renderApp("/devices", b);
    await screen.findByText("nas-1");
    const ws = sock(0);
    expect(ws.url).toMatch(/^wss?:\/\/.+\/api\/v1\/ws$/);
    act(() => ws.open());
    expect(await screen.findByRole("button", { name: /^Health: Healthy\b/ })).toBeInTheDocument();

    act(() =>
      ws.push({
        type: "instance_status",
        instance_id: "i-1",
        status: { state: "down", checked_at: null, last_success_at: null, error: "no answer" },
      }),
    );
    expect(await screen.findByText(/no answer/i)).toBeInTheDocument();

    const before = listFetches(b);
    act(() => ws.push({ type: "refresh", instance_id: "i-1" }));
    await vi.waitFor(() => expect(listFetches(b)).toBe(before + 1));
  });

  it("reconnects with backoff after a drop and says so", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      renderApp("/devices", backend());
      await screen.findByText("nas-1");
      act(() => sock(0).open());
      act(() => sock(0).drop());
      expect(screen.getByRole("button", { name: /^Health: Warning\b/ })).toBeInTheDocument();
      await act(() => vi.advanceTimersByTimeAsync(1_000));
      expect(FakeWebSocket.instances).toHaveLength(2);
      act(() => sock(1).drop());
      await act(() => vi.advanceTimersByTimeAsync(1_000));
      expect(FakeWebSocket.instances).toHaveLength(2); // the second wait is 2 s
      await act(() => vi.advanceTimersByTimeAsync(1_000));
      expect(FakeWebSocket.instances).toHaveLength(3);
    } finally {
      vi.useRealTimers();
    }
  });

  it("goes to the login page when the backend ends the session (4401), without reconnecting", async () => {
    const b = backend();
    renderApp("/devices", b);
    await screen.findByText("nas-1");
    act(() => sock(0).open());
    b.state.session = null; // the session ended on the server
    act(() => sock(0).drop(4401));
    expect(await screen.findByRole("heading", { level: 1, name: "Log in" })).toBeInTheDocument();
    expect(FakeWebSocket.instances).toHaveLength(1);
  });

  it("checks the session when the handshake is refused", async () => {
    const b = backend();
    renderApp("/devices", b);
    await screen.findByText("nas-1");
    b.state.session = null;
    act(() => sock(0).drop()); // closed before it ever opened: refused or unreachable
    expect(await screen.findByRole("heading", { level: 1, name: "Log in" })).toBeInTheDocument();
  });

  it("stores nothing in localStorage", async () => {
    renderApp("/devices", backend());
    await screen.findByText("nas-1");
    act(() => sock(0).open());
    expect(localStorage.length).toBe(0);
  });
});

describe("live rates", () => {
  it("keeps the pushed throughput rate, and ignores a malformed one", () => {
    const queryClient = new QueryClient();
    applyMessage(
      queryClient,
      JSON.stringify({ type: "live_rate", in_bps: 5000, out_bps: 2000, ports: 1, at: "x" }),
    );
    expect(queryClient.getQueryData(LIVE_RATE_KEY)).toMatchObject({ inBps: 5000, outBps: 2000 });
    applyMessage(queryClient, JSON.stringify({ type: "live_rate", in_bps: "lots" }));
    expect(queryClient.getQueryData(LIVE_RATE_KEY)).toMatchObject({ inBps: 5000 });
  });
});
