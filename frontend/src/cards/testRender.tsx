import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import type { ComponentType } from "react";
import { ApiClient } from "../api";
import type { Instance, QueryResult } from "../api";
import { json } from "../test-utils";
import type { CardProps } from "./registry";

export const testInstance = {
  id: "inst-1",
  name: "test-device",
  status: { state: "up", checked_at: null, last_success_at: null, error: null },
} as Instance;

/** A vector result: one series per `[labels, value]`. */
export function vector(rows: [Record<string, string>, string][]): QueryResult {
  return {
    result_type: "vector",
    series: rows.map(([labels, value]) => ({ labels, values: [[1_700_000_000, value]] })),
    warnings: [],
  };
}

/**
 * Render a card whose queries are answered by `answer(promql)`. `client` is the ApiClient
 * the test's `vi.mock("../../app/session")` returns.
 */
export function renderCard(
  client: ApiClient,
  Card: ComponentType<CardProps>,
  answer: (query: string) => QueryResult,
) {
  const fetchMock = vi.fn<typeof fetch>(async (_url, init) => {
    const { query } = JSON.parse(String(init?.body)) as { query: string };
    return json(answer(query));
  });
  vi.spyOn(client, "query").mockImplementation((req) =>
    new ApiClient({ fetch: fetchMock }).query(req),
  );
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <Card instanceId="inst-1" settings={{}} title="Card" instance={testInstance} />
    </QueryClientProvider>,
  );
}
