import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { ApiClient } from "../../api";
import type { QueryResult } from "../../api";
import { json } from "../../test-utils";
import { selectorFor } from "./promql";
import { rangeWindow, useInstantQuery, useRangeQuery } from "./useMetricQuery";

const client = new ApiClient({ fetch: vi.fn() });
vi.mock("../../app/session", () => ({ useApiClient: () => client }));

const RESULT: QueryResult = { result_type: "vector", series: [], warnings: [] };

function setup() {
  const fetchMock = vi.fn<typeof fetch>(async () => json(RESULT));
  const scoped = new ApiClient({ fetch: fetchMock });
  vi.spyOn(client, "query").mockImplementation((req) => scoped.query(req));
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
  const sent = () => fetchMock.mock.calls.map(([, init]) => JSON.parse(String(init?.body)));
  return { wrapper, sent };
}

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
});

describe("query hooks", () => {
  it("send an instant query scoped to the instance", async () => {
    const { wrapper, sent } = setup();
    const { result } = renderHook(() => useInstantQuery("inst-1", (sel) => `up${sel('job="x"')}`), {
      wrapper,
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(sent()).toEqual([{ query: 'up{vantage_instance_id="inst-1",job="x"}' }]);
  });

  it("send a range query with start, end and step", async () => {
    const { wrapper, sent } = setup();
    const { result } = renderHook(() => useRangeQuery("inst-1", (sel) => `up${sel()}`, 600), {
      wrapper,
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    const [body] = sent();
    expect(body.query).toBe('up{vantage_instance_id="inst-1"}');
    expect(body.end - body.start).toBe(600);
    expect(body.step).toBe(10);
  });
});
