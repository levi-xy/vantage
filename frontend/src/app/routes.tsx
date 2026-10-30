import type { RouteObject } from "react-router";
import { AccountPage } from "../pages/AccountPage";
import { AlertsPage } from "../pages/AlertsPage";
import { AuditPage } from "../pages/AuditPage";
import { DashboardPage } from "../pages/DashboardPage";
import { DevicesPage } from "../pages/DevicesPage";
import { AddDevicePage, EditDevicePage } from "../pages/devices/DeviceFormPages";
import { LoginPage } from "../pages/LoginPage";
import { NotFoundPage } from "../pages/NotFoundPage";
import { SetupPage } from "../pages/SetupPage";
import { UsersPage } from "../pages/UsersPage";
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
        path: "alerts",
        element: (
          <RequireRole role="admin">
            <AlertsPage />
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
      {
        path: "users",
        element: (
          <RequireRole role="admin">
            <UsersPage />
          </RequireRole>
        ),
      },
      { path: "account", element: <AccountPage /> },
      { path: "*", element: <NotFoundPage /> },
    ],
  },
];
