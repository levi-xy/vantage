import type { RouteObject } from "react-router";
import { AuditPage } from "../pages/AuditPage";
import { DashboardPage } from "../pages/DashboardPage";
import { DevicesPage } from "../pages/DevicesPage";
import { AddDevicePage, EditDevicePage } from "../pages/devices/DeviceFormPages";
import { LoginPage } from "../pages/LoginPage";
import { NotFoundPage } from "../pages/NotFoundPage";
import { SetupPage } from "../pages/SetupPage";
import { RequireRole, SessionGate } from "./gates";
import { Layout } from "./Layout";

/** Shared by the browser router (main.tsx) and the memory routers used in tests. */
export const routes: RouteObject[] = [
  {
    path: "setup",
    element: (
      <SessionGate only="setup">
        <SetupPage />
      </SessionGate>
    ),
  },
  {
    path: "login",
    element: (
      <SessionGate only="anonymous">
        <LoginPage />
      </SessionGate>
    ),
  },
  {
    element: (
      <SessionGate only="authenticated">
        <Layout />
      </SessionGate>
    ),
    children: [
      { index: true, element: <DashboardPage /> },
      { path: "devices", element: <DevicesPage /> },
      {
        path: "devices/new",
        element: (
          <RequireRole role="admin">
            <AddDevicePage />
          </RequireRole>
        ),
      },
      {
        path: "devices/:id",
        element: (
          <RequireRole role="admin">
            <EditDevicePage />
          </RequireRole>
        ),
      },
      {
        path: "audit",
        element: (
          <RequireRole role="admin">
            <AuditPage />
          </RequireRole>
        ),
      },
      { path: "*", element: <NotFoundPage /> },
    ],
  },
];
