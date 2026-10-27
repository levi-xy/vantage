import { QueryClientProvider } from "@tanstack/react-query";
import type { QueryClient } from "@tanstack/react-query";
import { RouterProvider } from "react-router";
import type { DataRouter } from "react-router";
import { api } from "../api";
import type { ApiClient } from "../api";
import { SessionProvider } from "./session";
import { ThemeProvider } from "./ThemeProvider";

interface AppProps {
  router: DataRouter;
  queryClient: QueryClient;
  /** Tests pass a client with a fake fetch. */
  client?: ApiClient;
}

/** Providers around the router. main.tsx passes a browser router; tests pass memory routers. */
export function App({ router, queryClient, client = api }: AppProps) {
  return (
    <QueryClientProvider client={queryClient}>
      <ThemeProvider>
        <SessionProvider client={client}>
          <RouterProvider router={router} />
        </SessionProvider>
      </ThemeProvider>
    </QueryClientProvider>
  );
}
