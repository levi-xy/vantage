import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { ApiClient } from "../../api";
import type { Instance, QueryRequest, QueryResult } from "../../api";
import { json } from "../../test-utils";
import { testInstance } from "../testRender";
import { NO_RECENT_DATA } from "./freshness";
import { selectorFor } from "./promql";
import { rangeWindow, useInstantQuery, useRangeQuery } from "./useMetricQuery";

const client = new ApiClient({ fetch: vi.fn() });
vi.mock("../../app/session", () => ({ useApiClient: () => client }));

const EMPTY: QueryResult = { result_type: "vector", series: [], warnings: [] };
const one = (value: string, t = 1_700_000_000): QueryResult => ({
  result_type: "vector",
  series: [{ labels: { vantage_instance_id: "inst-1" }, values: [[t, value]] }],
  warnings: [],
});

function setup(
  answer: (req: QueryRequest) => QueryResult = () => EMPTY,
  instance?: Instance,
  listUpdatedAtMs?: number,
) {
  const fetchMock = vi.fn<typeof fetch>(async (_url, init) =>
    json(answer(JSON.parse(String(init?.body)) as QueryRequest)),
  );
  const scoped = new ApiClient({ fetch: fetchMock });
  vi.spyOn(client, "query").mockImplementation((req) => scoped.query(req));
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  if (instance) {
    queryClient.setQueryData(["instances"], [instance], { updatedAt: listUpdatedAtMs });
  }
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
  const sent = () =>
    fetchMock.mock.calls.map(([, init]) => JSON.parse(String(init?.body)) as QueryRequest);
  return { wrapper, sent };
}

const nowSeconds = () => Math.floor(Date.now() / 1000);
const polled = (state: Instance["status"]["state"], lastSuccess: number | null): Instance => ({
  ...testInstance,
  enabled: true,
  status: {
    state,
    checked_at: null,
    last_success_at: lastSuccess === null ? null : new Date(lastSuccess * 1000).toISOString(),
    error: null,
  },
});

const CARD_QUERY = 'vantage_ping_check_rtt_avg_seconds{vantage_instance_id="inst-1"}';
const build = (sel: (extra?: string) => string) => `vantage_ping_check_rtt_avg_seconds${sel()}`;
/** A one-point range query: the card's query evaluated at a past moment. */
const isPast = (req: QueryRequest) => req.start !== undefined && req.start === req.end;

describe("selectorFor", () => {
  it("always carries the instance id and escapes it", () => {
    expect(selectorFor("abc")()).toBe('{vantage_instance_id="abc"}');
    expect(selectorFor("abc")('mode="idle"')).toBe('{vantage_instance_id="abc",mode="idle"}');
    expect(selectorFor('a"b}')()).toBe('{vantage_instance_id="a\\"b}"}');
  });
});

describe("rangeWindow", () => {
  it("ends on a step boundary and spans the window", () => {
    const w = rangeWindow(3600, 1_700_000_123_000);
    expect(w.step).toBe(60);
    expect(w.end % w.step).toBe(0);
    expect(w.end - w.start).toBe(3600);
  });

  it("never uses a step below the proxy's 15 s minimum", () => {
    expect(rangeWindow(300, 1_700_000_000_000).step).toBe(15);
  });
});

describe("query hooks", () => {
  it("send an instant query scoped to the instance", async () => {
    const { wrapper, sent } = setup();
    const { result } = renderHook(() => useInstantQuery("inst-1", (sel) => `up${sel('job="x"')}`), {
      wrapper,
    });
    await waitFor(() => expect(result.current.isPending).toBe(false));
    expect(sent()).toEqual([{ query: 'up{vantage_instance_id="inst-1",job="x"}' }]);
  });

  it("send a range query with start, end and step", async () => {
    const { wrapper, sent } = setup();
    const { result } = renderHook(() => useRangeQuery("inst-1", (sel) => `up${sel()}`, 600), {
      wrapper,
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    const [body] = sent();
    expect(body?.query).toBe('up{vantage_instance_id="inst-1"}');
    expect((body?.end ?? 0) - (body?.start ?? 0)).toBe(600);
    expect(body?.step).toBe(15);
  });
});

describe("stale values", () => {
  it("shows a live result from a fresh source as fresh", async () => {
    const { wrapper, sent } = setup(() => one("0.01"), polled("up", nowSeconds() - 30));
    const { result } = renderHook(() => useInstantQuery("inst-1", build), { wrapper });
    await waitFor(() => expect(result.current.data?.series).toHaveLength(1));
    expect(result.current.freshness).toEqual({ kind: "fresh" });
    expect(sent()).toEqual([{ query: CARD_QUERY }]);
  });

  it("re-runs the identical query one scrape after last_success_at when live is empty", async () => {
    const last = nowSeconds() - 60;
    const { wrapper, sent } = setup(
      (req) => (isPast(req) ? one("0.02", last + 30) : EMPTY),
      polled("up", last),
    );
    const { result } = renderHook(() => useInstantQuery("inst-1", build), { wrapper });
    await waitFor(() => expect(result.current.freshness.kind).toBe("stale"));
    // "as of" is the poll itself; the query runs when that poll's samples were in Prometheus.
    expect(result.current.freshness).toEqual({ kind: "stale", asOf: last });
    expect(result.current.data?.series[0]?.values[0]?.[1]).toBe("0.02");
    expect(sent()).toEqual([
      { query: CARD_QUERY },
      { query: CARD_QUERY, start: last + 30, end: last + 30, step: 15 },
    ]);
  });

  it("never evaluates in the future when the last success is under 30 s old", async () => {
    const last = nowSeconds() - 10;
    const { wrapper, sent } = setup(
      (req) => (isPast(req) ? one("0.05", last) : EMPTY),
      polled("up", last),
    );
    const { result } = renderHook(() => useInstantQuery("inst-1", build), { wrapper });
    await waitFor(() => expect(result.current.freshness.kind).toBe("stale"));
    const past = sent().find(isPast);
    expect(past?.start).toBe(past?.end);
    // Clamped to the live query's answer time (now), not last + 30.
    expect(past?.start).toBeGreaterThanOrEqual(last + 10);
    expect(past?.start).toBeLessThan(last + 30);
    expect(result.current.freshness).toEqual({ kind: "stale", asOf: last });
  });

  it("doesn't grey a live value when the instances list lags behind", async () => {
    const last = nowSeconds() - 600;
    // The list was last updated 60 s after that success: it couldn't know of later ones.
    const { wrapper, sent } = setup(() => one("0.01"), polled("up", last), (last + 60) * 1000);
    const { result } = renderHook(() => useInstantQuery("inst-1", build), { wrapper });
    await waitFor(() => expect(result.current.data?.series).toHaveLength(1));
    expect(result.current.freshness).toEqual({ kind: "fresh" });
    expect(sent()).toEqual([{ query: CARD_QUERY }]);
  });

  it("uses the past value when the source is stale, even if the live result isn't empty", async () => {
    const last = nowSeconds() - 600;
    const { wrapper } = setup(
      (req) => (isPast(req) ? one("0.03", last) : one("0.09")),
      polled("down", last),
    );
    const { result } = renderHook(() => useInstantQuery("inst-1", build), { wrapper });
    await waitFor(() => expect(result.current.freshness.kind).toBe("stale"));
    expect(result.current.data?.series[0]?.values[0]?.[1]).toBe("0.03");
  });

  it("says there is no recent data without a last_success_at", async () => {
    const { wrapper, sent } = setup(() => EMPTY, polled("down", null));
    const { result } = renderHook(() => useInstantQuery("inst-1", build), { wrapper });
    await waitFor(() => expect(result.current.freshness.kind).toBe("missing"));
    expect(result.current.freshness).toEqual({ kind: "missing", note: NO_RECENT_DATA });
    expect(sent()).toHaveLength(1);
  });

  it("finds a scraped target's last up == 1 (1 h miss, 24 h hit) and evaluates there", async () => {
    const lastUp = nowSeconds() - 7200;
    const { wrapper, sent } = setup(
      (req) => {
        if (req.query.startsWith("max by")) {
          return one("0"); // the shared scrape state: the target is down
        }
        if (req.query.endsWith("== 1")) {
          const span = (req.end ?? 0) - (req.start ?? 0);
          return span <= 3600 ? EMPTY : one("1", lastUp);
        }
        return isPast(req) ? one("0.04", lastUp) : EMPTY;
      },
      polled("unknown", null),
    );
    const { result } = renderHook(() => useInstantQuery("inst-1", build), { wrapper });
    await waitFor(() => expect(result.current.freshness.kind).toBe("stale"));
    expect(result.current.freshness).toEqual({ kind: "stale", asOf: lastUp });

    const lookups = sent().filter((r) => r.query.endsWith("== 1"));
    expect(lookups.map((r) => (r.end ?? 0) - (r.start ?? 0))).toEqual([3600, 86400]);
    expect(lookups[0]?.query).toBe('up{vantage_instance_id="inst-1"} == 1');
    expect(sent()).toContainEqual({ query: CARD_QUERY, start: lastUp, end: lastUp, step: 15 });
  });
});
