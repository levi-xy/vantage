import type { RouteObject } from "react-router";
import { AuditPage } from "../pages/AuditPage";
import { DashboardPage } from "../pages/DashboardPage";
import { DevicesPage } from "../pages/DevicesPage";
import { NotFoundPage } from "../pages/NotFoundPage";
import { Layout } from "./Layout";

/** Shared by the browser router (main.tsx) and the memory routers used in tests. */
export const routes: RouteObject[] = [
  {
    element: <Layout />,
    children: [
      { index: true, element: <DashboardPage /> },
      { path: "devices", element: <DevicesPage /> },
      { path: "audit", element: <AuditPage /> },
      { path: "*", element: <NotFoundPage /> },
    ],
  },
];
