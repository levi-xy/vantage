import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import type { ComponentType } from "react";
import { ApiClient } from "../api";
import type { Instance, QueryRequest, QueryResult } from "../api";
import { json } from "../test-utils";
import type { CardProps } from "./registry";
import { CardTierContext } from "./metrics/tier";
import type { CardTier } from "./metrics/tier";

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
 * the test's `vi.mock("../../app/session")` returns. `answer` also gets the request, for
 * range queries. With `cached`, the instance is in the instances list the dashboard keeps
 * (where the query hooks read `last_success_at` from). With `tier`, the card is shown at that
 * tier, as the dashboard grid does for a card below its full size.
 */
export function renderCard(
  client: ApiClient,
  Card: ComponentType<CardProps>,
  answer: (query: string, request: QueryRequest) => QueryResult,
  instance: Instance = testInstance,
  {
    cached = false,
    tier = "full",
    settings = {},
  }: { cached?: boolean; tier?: CardTier; settings?: CardProps["settings"] } = {},
) {
  const fetchMock = vi.fn<typeof fetch>(async (_url, init) => {
    const request = JSON.parse(String(init?.body)) as QueryRequest;
    return json(answer(request.query, request));
  });
  vi.spyOn(client, "query").mockImplementation((req) =>
    new ApiClient({ fetch: fetchMock }).query(req),
  );
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  if (cached) queryClient.setQueryData(["instances"], [instance]);
  render(
    <QueryClientProvider client={queryClient}>
      <CardTierContext.Provider value={{ tier }}>
        <Card instanceId="inst-1" settings={settings} title="Card" instance={instance} />
      </CardTierContext.Provider>
    </QueryClientProvider>,
  );
}
