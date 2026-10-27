import { QueryClientProvider } from "@tanstack/react-query";
import type { QueryClient } from "@tanstack/react-query";
import { RouterProvider } from "react-router";
import type { DataRouter } from "react-router";
import { ThemeProvider } from "./ThemeProvider";

interface AppProps {
  router: DataRouter;
  queryClient: QueryClient;
}

/** Providers around the router. main.tsx passes a browser router; tests pass memory routers. */
export function App({ router, queryClient }: AppProps) {
  return (
    <QueryClientProvider client={queryClient}>
      <ThemeProvider>
        <RouterProvider router={router} />
      </ThemeProvider>
    </QueryClientProvider>
  );
}
