import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { NavLink, Outlet } from "react-router";
import type { Role } from "../api";
import { ThemeToggle } from "../components/ThemeToggle";
import { buttonSecondary } from "../components/styles";
import { useLiveUpdates } from "./live";
import type { LiveState } from "./live";
import { useSession, useUser } from "./session";

interface NavItem {
  to: string;
  label: string;
  end: boolean;
  /** Only users with this role see the link. The route and the backend check it too. */
  role?: Role;
}

const NAV: readonly NavItem[] = [
  { to: "/", label: "Dashboard", end: true },
  { to: "/devices", label: "Devices", end: false },
  { to: "/users", label: "Users", end: false, role: "admin" },
  { to: "/audit", label: "Audit log", end: false, role: "admin" },
  { to: "/account", label: "My account", end: false },
];

const ROLE_LABELS: Record<Role, string> = { admin: "Admin", viewer: "Viewer" };

/** Whether live pushes are flowing. Text as well as colour. */
function LiveIndicator({ state }: { state: LiveState }) {
  const live = state === "live";
  return (
    <span className="flex items-center gap-1.5 text-xs text-slate-500 dark:text-slate-400">
      <span
        aria-hidden="true"
        className={`h-2 w-2 rounded-full ${live ? "bg-emerald-500" : "bg-amber-500"}`}
      />
      {live ? "Live" : "Reconnecting"}
    </span>
  );
}

/** The user's name and role, and the logout button. */
function UserMenu() {
  const user = useUser();
  const { logout } = useSession();
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);

  async function onLogout() {
    setBusy(true);
    setFailed(false);
    try {
      // On success the session gate sends the user to the login page.
      await logout();
    } catch {
      setFailed(true);
      setBusy(false);
    }
  }

  return (
    <div className="flex items-center gap-3">
      <span className="hidden text-sm sm:inline">
        <span className="font-medium">{user.username}</span>{" "}
        <span className="text-slate-500 dark:text-slate-400">({ROLE_LABELS[user.role]})</span>
      </span>
      {failed && (
        <span role="alert" className="text-xs text-red-700 dark:text-red-400">
          Logout failed
        </span>
      )}
      <button type="button" onClick={onLogout} disabled={busy} className={buttonSecondary}>
        Log out
      </button>
    </div>
  );
}

function navClass({ isActive }: { isActive: boolean }): string {
  const base =
    "block rounded-md px-3 py-2 text-sm font-medium focus-visible:ring-2 focus-visible:ring-sky-500 focus-visible:outline-none";
  return isActive
    ? `${base} bg-slate-200 text-slate-900 dark:bg-slate-800 dark:text-white`
    : `${base} text-slate-600 hover:bg-slate-100 hover:text-slate-900 dark:text-slate-300 dark:hover:bg-slate-800 dark:hover:text-white`;
}

/** Header + navigation around every page. Sidebar on desktop, collapsible menu on phones. */
export function Layout() {
  const [menuOpen, setMenuOpen] = useState(false);
  const user = useUser();
  const live = useLiveUpdates(useQueryClient());
  const nav = NAV.filter((item) => item.role === undefined || item.role === user.role);

  return (
    <div className="min-h-screen bg-slate-50 text-slate-900 dark:bg-slate-950 dark:text-slate-100">
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:absolute focus:top-2 focus:left-2 focus:rounded focus:bg-white focus:px-3 focus:py-2 dark:focus:bg-slate-900"
      >
        Skip to content
      </a>
      <header className="sticky top-0 z-10 border-b border-slate-200 bg-white/90 backdrop-blur dark:border-slate-800 dark:bg-slate-900/90">
        <div className="flex h-14 items-center gap-3 px-4">
          <button
            type="button"
            className="inline-flex h-9 w-9 items-center justify-center rounded-md border border-slate-300 md:hidden dark:border-slate-700"
            aria-label={menuOpen ? "Close menu" : "Open menu"}
            aria-expanded={menuOpen}
            aria-controls="main-nav"
            onClick={() => setMenuOpen((open) => !open)}
          >
            <span aria-hidden="true">{menuOpen ? "✕" : "☰"}</span>
          </button>
          <span className="text-lg font-semibold tracking-tight">Vantage</span>
          <div className="ml-auto flex items-center gap-3">
            <LiveIndicator state={live} />
            <ThemeToggle />
            <UserMenu />
          </div>
        </div>
      </header>

      <div className="md:flex">
        <nav
          id="main-nav"
          aria-label="Main"
          className={`${menuOpen ? "block" : "hidden"} border-b border-slate-200 bg-white p-3 md:sticky md:top-14 md:block md:h-[calc(100vh-3.5rem)] md:w-56 md:shrink-0 md:border-r md:border-b-0 dark:border-slate-800 dark:bg-slate-900`}
        >
          <ul className="space-y-1">
            {nav.map((item) => (
              <li key={item.to}>
                <NavLink
                  to={item.to}
                  end={item.end}
                  className={navClass}
                  onClick={() => setMenuOpen(false)}
                >
                  {item.label}
                </NavLink>
              </li>
            ))}
          </ul>
        </nav>

        <main id="main" className="min-w-0 flex-1 p-4 md:p-6">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
