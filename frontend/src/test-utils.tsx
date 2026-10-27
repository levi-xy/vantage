import { render } from "@testing-library/react";
import { createMemoryRouter } from "react-router";
import { App } from "./app/App";
import { createQueryClient } from "./app/queryClient";
import { routes } from "./app/routes";

/** A controllable `prefers-color-scheme: dark` media query (jsdom has no matchMedia). */
export function mockSystemTheme(dark: boolean) {
  const listeners = new Set<() => void>();
  const query = {
    matches: dark,
    media: "(prefers-color-scheme: dark)",
    addEventListener: (_type: string, fn: () => void) => listeners.add(fn),
    removeEventListener: (_type: string, fn: () => void) => listeners.delete(fn),
  };
  vi.stubGlobal(
    "matchMedia",
    vi.fn(() => query),
  );
  return {
    set(next: boolean) {
      query.matches = next;
      for (const fn of listeners) fn();
    },
    listenerCount: () => listeners.size,
  };
}

export function renderApp(path = "/") {
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  const result = render(<App router={router} queryClient={createQueryClient()} />);
  return { ...result, router };
}
