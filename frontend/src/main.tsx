import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { createBrowserRouter } from "react-router";
import { App } from "./app/App";
import { createQueryClient } from "./app/queryClient";
import { routes } from "./app/routes";
import { applyTheme, readPreference, resolveTheme } from "./app/theme";
import "./index.css";

// Before the first render, so the page doesn't flash light in dark mode.
applyTheme(resolveTheme(readPreference()));

const root = document.getElementById("root");
if (!root) throw new Error("Missing #root element");

createRoot(root).render(
  <StrictMode>
    <App router={createBrowserRouter(routes)} queryClient={createQueryClient()} />
  </StrictMode>,
);
