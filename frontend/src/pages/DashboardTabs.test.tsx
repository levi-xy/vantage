import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { exampleMe, fakeBackend, json, mockSystemTheme, renderApp } from "../test-utils";

beforeEach(() => {
  mockSystemTheme(false);
});

const LIST = [
  { id: 3, name: "Main", position: 0 },
  { id: 5, name: "Rack", position: 1 },
];
const empty = (id: number, name: string) => json({ id, name, layout: { version: 2, cards: [] } });

function backend(role: "admin" | "viewer" = "viewer") {
  return fakeBackend({
    session: exampleMe(role),
    routes: {
      "GET /dashboards": () => json(LIST),
      "GET /dashboards/3": () => empty(3, "Main"),
      "GET /dashboards/5": () => empty(5, "Rack"),
      "GET /dashboards/9": () => json({ detail: "dashboard not found" }, 404),
      "POST /dashboards": (body) =>
        json({ id: 7, ...(body as object), layout: { cards: [] } }, 201),
      "GET /dashboards/7": () => empty(7, "Copy"),
      "PATCH /dashboards/5": () => json({ id: 5, name: "Rack", position: 0 }),
      "DELETE /dashboards/5": () => new Response(null, { status: 204 }),
    },
  });
}

function bodyOf(b: ReturnType<typeof backend>, method: string, path: string): unknown {
  const call = b.fetch.mock.calls.find(
    ([input, init]) => init?.method === method && String(input).endsWith(path),
  );
  return call?.[1]?.body ? JSON.parse(String(call[1].body)) : undefined;
}

describe("dashboard tabs", () => {
  it("shows the first dashboard at / and links the others to /d/<id>", async () => {
    const b = backend();
    renderApp("/", b);
    await screen.findByText("The dashboard is empty.");
    const tabs = screen.getByRole("navigation", { name: "Dashboards" });
    const main = within(tabs).getByRole("link", { name: "Main" });
    expect(main).toHaveAttribute("href", "/");
    expect(main).toHaveAttribute("aria-current", "page");
    expect(within(tabs).getByRole("link", { name: "Rack" })).toHaveAttribute("href", "/d/5");
    expect(b.requests().map((r) => r.route)).toContain("GET /dashboards/3");
  });

  it("opens another dashboard by its address", async () => {
    renderApp("/d/5", backend());
    const tabs = await screen.findByRole("navigation", { name: "Dashboards" });
    expect(await within(tabs).findByRole("link", { name: "Rack" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(await screen.findByText("The dashboard is empty.")).toBeInTheDocument();
  });

  it("shows Dashboard not found for an unknown id, with a link to the main one", async () => {
    renderApp("/d/9", backend());
    expect(await screen.findByText("Dashboard not found.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open the main dashboard" })).toHaveAttribute(
      "href",
      "/",
    );
  });

  it("creates a copy of the current dashboard and opens it", async () => {
    const b = backend();
    const { router } = renderApp("/d/5", b);
    await screen.findByText("The dashboard is empty.");
    fireEvent.click(screen.getByRole("button", { name: "New dashboard" }));
    const form = screen.getByRole("form", { name: "New dashboard" });
    fireEvent.change(within(form).getByLabelText("Name"), { target: { value: " Copy " } });
    fireEvent.click(within(form).getByLabelText("Copy of Rack"));
    fireEvent.click(within(form).getByRole("button", { name: "Create" }));
    await waitFor(() => expect(router.state.location.pathname).toBe("/d/7"));
    expect(bodyOf(b, "POST", "/dashboards")).toEqual({ name: "Copy", copy_from: 5 });
  });

  it("in edit mode moves and deletes the current dashboard", async () => {
    const b = backend();
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    const { router } = renderApp("/d/5", b);
    await screen.findByText("The dashboard is empty.");
    expect(screen.queryByRole("button", { name: "Move left" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Add your first card" }));

    expect(screen.getByRole("button", { name: "Move right" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Move left" }));
    await waitFor(() => expect(bodyOf(b, "PATCH", "/dashboards/5")).toEqual({ position: 0 }));

    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    expect(confirm).toHaveBeenCalled();
    await waitFor(() => expect(router.state.location.pathname).toBe("/"));
    expect(b.requests().map((r) => r.route)).toContain("DELETE /dashboards/5");
    confirm.mockRestore();
  });

  it("on a phone: a select of the dashboards", async () => {
    const { router } = renderApp("/d/3", backend());
    const select = await screen.findByRole("combobox", { name: "Dashboard" });
    await within(select).findByRole("option", { name: "Rack" });
    fireEvent.change(select, { target: { value: "5" } });
    await waitFor(() => expect(router.state.location.pathname).toBe("/d/5"));
  });
});
