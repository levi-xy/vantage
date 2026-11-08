import { Navigate } from "react-router";
import type { RouteObject } from "react-router";
import { AccountPage } from "../pages/AccountPage";
import { AlertsPage } from "../pages/AlertsPage";
import { AuditPage } from "../pages/AuditPage";
import { DashboardPage } from "../pages/DashboardPage";
import { DevicesPage } from "../pages/DevicesPage";
import { DeviceDetailPage } from "../pages/devices/DeviceDetailPage";
import { AddDevicePage, EditDevicePage } from "../pages/devices/DeviceFormPages";
import { LoginPage } from "../pages/LoginPage";
import { MapPage } from "../pages/MapPage";
import { NotFoundPage } from "../pages/NotFoundPage";
import { ChannelsPage } from "../pages/ChannelsPage";
import { SettingsShell } from "../pages/settings/SettingsShell";
import { TimePage } from "../pages/TimePage";
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
      { path: "d/:id", element: <DashboardPage /> },
      { path: "map", element: <MapPage /> },
      { path: "map/:id", element: <MapPage /> },
      { path: "devices", element: <DevicesPage /> },
      {
        path: "devices/new",
        element: (
          <RequireRole role="admin">
            <AddDevicePage />
          </RequireRole>
        ),
      },
      { path: "devices/:id", element: <DeviceDetailPage /> },
      {
        path: "devices/:id/edit",
        element: (
          <RequireRole role="admin">
            <EditDevicePage />
          </RequireRole>
        ),
      },
      {
        path: "alerts",
        element: <AlertsPage />,
      },
      {
        path: "settings",
        element: (
          <RequireRole role="admin">
            <SettingsShell />
          </RequireRole>
        ),
        children: [
          { index: true, element: <Navigate to="/settings/time" replace /> },
          { path: "time", element: <TimePage /> },
          { path: "users", element: <UsersPage /> },
          { path: "audit", element: <AuditPage /> },
          { path: "channels", element: <ChannelsPage /> },
        ],
      },
      { path: "users", element: <Navigate to="/settings/users" replace /> },
      { path: "audit", element: <Navigate to="/settings/audit" replace /> },
      { path: "account", element: <AccountPage /> },
      { path: "*", element: <NotFoundPage /> },
    ],
  },
];
