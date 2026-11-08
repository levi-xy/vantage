import { fireEvent, screen, waitFor } from "@testing-library/react";
import { exampleMe, fakeBackend, json, mockSystemTheme, renderApp } from "../test-utils";

beforeEach(() => {
  mockSystemTheme(false);
});

function timeBackend(serverOffsetMs = 0) {
  const state = { timezone: "UTC" };
  const view = () => ({
    timezone: state.timezone,
    server_time: new Date(Date.now() + serverOffsetMs).toISOString(),
    clock_source: "host",
    timezones: ["UTC", "Europe/Berlin", "Asia/Tokyo"],
  });
  return fakeBackend({
    session: exampleMe("admin"),
    routes: {
      "GET /settings/time": () => json(view()),
      "PUT /settings/time": (body) => {
        state.timezone = (body as { timezone: string }).timezone;
        return json(view());
      },
    },
  });
}

describe("settings: time", () => {
  it("saves the chosen timezone", async () => {
    const backend = timeBackend();
    renderApp("/settings", backend);
    const select = await screen.findByLabelText<HTMLSelectElement>("Timezone");
    expect(select.value).toBe("UTC");
    expect(screen.getByText(/Keep NTP running on the host/)).toBeInTheDocument();
    expect(screen.queryByText(/server clock is/)).not.toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Search timezones"), { target: { value: "berlin" } });
    fireEvent.change(select, { target: { value: "Europe/Berlin" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => {
      const put = backend.fetch.mock.calls.find(([, init]) => init?.method === "PUT");
      expect(JSON.parse(String(put?.[1]?.body))).toEqual({ timezone: "Europe/Berlin" });
    });
    await waitFor(() =>
      expect(screen.getByLabelText<HTMLSelectElement>("Timezone").value).toBe("Europe/Berlin"),
    );
  });

  it("warns when the server clock differs from this browser's", async () => {
    renderApp("/settings/time", timeBackend(-42_000));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      /The server clock is 4\d s behind this browser. Check NTP on the host./,
    );
  });
});
