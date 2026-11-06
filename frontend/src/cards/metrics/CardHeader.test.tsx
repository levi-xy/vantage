import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { exampleInstance, INSTANCE_ID } from "../../pages/devices/testFixtures";
import { CardHeader } from "./CardHeader";
import { CardEditingContext } from "./editing";

function renderHeader(editing: boolean) {
  render(
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter>
        <CardEditingContext.Provider value={editing}>
          <CardHeader instance={exampleInstance()} freshness={{ kind: "fresh" }} />
        </CardEditingContext.Provider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

vi.mock("../../app/session", () => ({
  useApiClient: () => ({ query: () => Promise.reject(new Error("no")) }),
}));

describe("card header name", () => {
  it("links to the device page", () => {
    renderHeader(false);
    expect(screen.getByRole("link", { name: "nas-1" })).toHaveAttribute(
      "href",
      `/devices/${INSTANCE_ID}`,
    );
  });

  it("is plain text while the dashboard is in edit mode", () => {
    renderHeader(true);
    expect(screen.queryByRole("link")).toBeNull();
    expect(screen.getByText("nas-1")).toBeInTheDocument();
  });
});
