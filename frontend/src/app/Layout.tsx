import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import type { ReactNode, RefObject } from "react";
import { Link, NavLink, Outlet } from "react-router";
import type { Role } from "../api";
import { OPEN_ALERTS_LIMIT, useOpenAlerts } from "../cards/openAlerts";
import { ThemeToggle } from "../components/ThemeToggle";
import { systemAlertText } from "./health";
import { useDeliveryStatus } from "./deliveryStatus";
import { HealthStatus } from "./HealthStatus";
import { useLastDataUpdate, useLiveUpdates } from "./live";
import { useSession, useUser } from "./session";

/** Shown to every logged-in user while a system alert fires (Prometheus unreachable, ...)
 * or alert messages wait 15+ minutes or recently failed. One poll for both. */
function DeliveryBanner() {
  const status = useDeliveryStatus();
  const stuck = status.data?.stuck ?? 0;
  const system = status.data?.system_alerts ?? [];
  return (
    <>
      {system.map((alert) => (
        <div
          key={alert.system_kind}
          role="alert"
          className="border-b border-red-300 bg-red-50 px-4 py-2 text-sm text-red-900 dark:border-red-800 dark:bg-red-950 dark:text-red-100"
        >
          {systemAlertText(alert.system_kind)}{" "}
          <Link to="/alerts" className="font-medium underline">
            See Alerts
          </Link>
        </div>
      ))}
      {stuck > 0 && (
        <div
          role="alert"
          className="border-b border-amber-300 bg-amber-50 px-4 py-2 text-sm text-amber-900 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-100"
        >
          {stuck === 1 ? "1 alert notification has" : `${stuck} alert notifications have`} failed or
          waited more than 15 minutes.{" "}
          <Link to="/alerts" className="font-medium underline">
            See Alerts
          </Link>
        </div>
      )}
    </>
  );
}

const ROLE_LABELS: Record<Role, string> = { admin: "Admin", viewer: "Viewer" };

/**
 * Open (firing or no data) alerts, for the tab badge. Refetched every refresh interval and
 * on every `/ws` alert push, so it is also the health status's heartbeat on every page.
 */
function useOpenAlertCount(): number {
  return useOpenAlerts().data?.length ?? 0;
}

const focusRing =
  "focus-visible:ring-2 focus-visible:ring-in focus-visible:outline-none focus-visible:ring-inset";

function Logo() {
  return (
    <svg width="24" height="24" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path
        d="M3 6l9 13 9-13"
        className="stroke-in"
        strokeWidth="2.4"
        strokeLinejoin="round"
        strokeLinecap="round"
      />
      <circle cx="12" cy="7" r="2.2" className="fill-out" />
    </svg>
  );
}

/** The open-alert count on the Alerts tab; nothing while there is none. */
function AlertBadge({ count, className = "" }: { count: number; className?: string }) {
  if (count <= 0) return null;
  return (
    <span
      className={`rounded-full bg-crit px-[7px] text-[11px] leading-[18px] font-semibold text-on-crit ${className}`}
    >
      <span className="sr-only">, </span>
      {count >= OPEN_ALERTS_LIMIT ? `${OPEN_ALERTS_LIMIT}+` : count}
      <span className="sr-only"> open</span>
    </span>
  );
}

function Icon({ children }: { children: ReactNode }) {
  return (
    <svg
      width="20"
      height="20"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      aria-hidden="true"
    >
      {children}
    </svg>
  );
}

interface Tab {
  to: string;
  label: string;
  end: boolean;
  icon: ReactNode;
}

/** Every user sees these. The admin pages and My account are in the account menu. */
const TABS: readonly Tab[] = [
  {
    to: "/",
    label: "Dashboard",
    end: true,
    icon: (
      <Icon>
        <rect x="3" y="3" width="7" height="7" rx="1.5" />
        <rect x="14" y="3" width="7" height="7" rx="1.5" />
        <rect x="3" y="14" width="7" height="7" rx="1.5" />
        <rect x="14" y="14" width="7" height="7" rx="1.5" />
      </Icon>
    ),
  },
  {
    to: "/devices",
    label: "Devices",
    end: false,
    icon: (
      <Icon>
        <rect x="2" y="7" width="20" height="10" rx="2" />
        <path d="M6 12h.01M10 12h.01" />
      </Icon>
    ),
  },
  {
    to: "/alerts",
    label: "Alerts",
    end: false,
    icon: (
      <Icon>
        <path d="M6 8a6 6 0 1 1 12 0c0 7 3 9 3 9H3s3-2 3-9" />
        <path d="M10.3 21a1.9 1.9 0 0 0 3.4 0" />
      </Icon>
    ),
  },
];

interface MenuItem {
  to: string;
  label: string;
  /** Only users with this role see the link. The route and the backend check it too. */
  role?: Role;
}

const MENU: readonly MenuItem[] = [
  { to: "/users", label: "Users", role: "admin" },
  { to: "/audit", label: "Audit log", role: "admin" },
  { to: "/account", label: "My account" },
];

/** Desktop: tabs in the top bar, the current one underlined. */
function TopNav({ alerts }: { alerts: number }) {
  return (
    <nav aria-label="Main" className="hidden gap-1 self-stretch md:flex">
      {TABS.map((tab) => (
        <NavLink
          key={tab.to}
          to={tab.to}
          end={tab.end}
          className={({ isActive }) =>
            `flex items-center gap-2 border-b-2 px-3.5 ${focusRing} ${
              isActive
                ? "border-in font-medium text-ink"
                : "border-transparent text-muted hover:text-ink"
            }`
          }
        >
          {tab.label}
          {tab.to === "/alerts" && <AlertBadge count={alerts} />}
        </NavLink>
      ))}
    </nav>
  );
}

/** Phone: a bottom tab bar. "More" opens the account menu. */
function BottomTabs({
  alerts,
  menuOpen,
  onMore,
  moreRef,
}: {
  alerts: number;
  menuOpen: boolean;
  onMore: () => void;
  moreRef: RefObject<HTMLButtonElement>;
}) {
  const tabClass = (active: boolean) =>
    `relative flex flex-col items-center justify-center gap-[3px] text-[11px] ${focusRing} ${
      active ? "text-in" : "text-muted"
    }`;
  return (
    <nav
      aria-label="Tabs"
      className="fixed inset-x-0 bottom-0 z-30 grid h-16 grid-cols-4 border-t border-line bg-panel md:hidden"
    >
      {TABS.map((tab) => (
        <NavLink
          key={tab.to}
          to={tab.to}
          end={tab.end}
          className={({ isActive }) => tabClass(isActive)}
        >
          {tab.icon}
          {tab.label}
          {tab.to === "/alerts" && (
            <AlertBadge count={alerts} className="absolute top-2 left-[54%]" />
          )}
        </NavLink>
      ))}
      <button
        ref={moreRef}
        type="button"
        aria-expanded={menuOpen}
        aria-controls="account-menu"
        onClick={onMore}
        className={tabClass(menuOpen)}
      >
        <Icon>
          <circle cx="5" cy="12" r="1.5" />
          <circle cx="12" cy="12" r="1.5" />
          <circle cx="19" cy="12" r="1.5" />
        </Icon>
        More
      </button>
    </nav>
  );
}

/**
 * The account menu: the admin pages, My account and Log out. Under the user button on
 * desktop, a sheet above the tab bar on phones.
 */
function AccountMenu({
  onClose,
  panelRef,
}: {
  onClose: () => void;
  panelRef: RefObject<HTMLDivElement>;
}) {
  const user = useUser();
  const { logout } = useSession();
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);
  const items = MENU.filter((item) => item.role === undefined || item.role === user.role);

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

  const itemClass = `block w-full px-4 py-2.5 text-left text-ink hover:bg-chip disabled:opacity-60 md:py-2 ${focusRing}`;
  return (
    <div
      id="account-menu"
      ref={panelRef}
      className="fixed inset-x-0 bottom-16 z-40 border-t border-line bg-panel py-2 shadow-lg md:absolute md:top-full md:right-0 md:bottom-auto md:left-auto md:mt-2 md:w-56 md:rounded-lg md:border"
    >
      <p className="px-4 pt-1 pb-2 text-xs text-muted">
        Signed in as {user.username} · {ROLE_LABELS[user.role]}
      </p>
      <nav aria-label="Account">
        <ul>
          {items.map((item) => (
            <li key={item.to}>
              <NavLink to={item.to} className={itemClass} onClick={onClose}>
                {item.label}
              </NavLink>
            </li>
          ))}
          <li className="mt-1 border-t border-line pt-1">
            <button type="button" onClick={onLogout} disabled={busy} className={itemClass}>
              Log out
            </button>
          </li>
        </ul>
      </nav>
      {failed && (
        <p role="alert" className="px-4 pt-1 text-xs text-crit-text">
          Logout failed
        </p>
      )}
    </div>
  );
}

/** Header + navigation around every page: a top bar, plus a bottom tab bar on phones. */
export function Layout() {
  const [menuOpen, setMenuOpen] = useState(false);
  const user = useUser();
  const queryClient = useQueryClient();
  const live = useLiveUpdates(queryClient);
  const lastUpdate = useLastDataUpdate(queryClient);
  const alerts = useOpenAlertCount();
  const panelRef = useRef<HTMLDivElement>(null);
  const userButtonRef = useRef<HTMLButtonElement>(null);
  const moreRef = useRef<HTMLButtonElement>(null);
  const toggleMenu = () => setMenuOpen((open) => !open);

  // Close the menu on Escape, or on a click outside it and its two buttons.
  useEffect(() => {
    if (!menuOpen) return;
    const onPointer = (event: MouseEvent) => {
      const target = event.target;
      if (!(target instanceof Node)) return;
      const inside = [panelRef, userButtonRef, moreRef].some((r) => r.current?.contains(target));
      if (!inside) setMenuOpen(false);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setMenuOpen(false);
    };
    document.addEventListener("mousedown", onPointer);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onPointer);
      document.removeEventListener("keydown", onKey);
    };
  }, [menuOpen]);

  return (
    <div className="min-h-screen bg-canvas text-ink">
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:absolute focus:top-2 focus:left-2 focus:z-50 focus:rounded focus:bg-panel focus:px-3 focus:py-2"
      >
        Skip to content
      </a>
      <header className="sticky top-0 z-30 border-b border-line bg-panel">
        <div className="mx-auto flex h-14 max-w-[1440px] items-center gap-4 px-4 md:h-[60px] md:gap-8 md:px-8">
          <Link
            to="/"
            className={`flex items-center gap-2.5 text-[17px] font-semibold tracking-[-0.01em] ${focusRing}`}
          >
            <Logo />
            <span>Vantage</span>
          </Link>
          <TopNav alerts={alerts} />
          <div className="relative ml-auto flex items-center gap-3">
            <HealthStatus connection={live} lastUpdate={lastUpdate} />
            <ThemeToggle />
            <button
              ref={userButtonRef}
              type="button"
              aria-expanded={menuOpen}
              aria-controls="account-menu"
              onClick={toggleMenu}
              className={`hidden h-9 items-center gap-2 rounded-lg border border-line pr-2.5 pl-1.5 text-ink hover:bg-chip md:flex ${focusRing}`}
            >
              <span
                aria-hidden="true"
                className="flex h-6 w-6 items-center justify-center rounded-full bg-chip text-[11px] font-semibold uppercase"
              >
                {user.username.slice(0, 1)}
              </span>
              <span>{user.username}</span>
              <span className="sr-only"> ({ROLE_LABELS[user.role]})</span>
              <svg
                width="12"
                height="12"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
                aria-hidden="true"
              >
                <path d="M6 9l6 6 6-6" />
              </svg>
            </button>
            {menuOpen && <AccountMenu onClose={() => setMenuOpen(false)} panelRef={panelRef} />}
          </div>
        </div>
      </header>
      <DeliveryBanner />

      <main
        id="main"
        className="mx-auto w-full max-w-[1440px] min-w-0 px-4 pt-4 pb-24 md:px-8 md:pt-7 md:pb-12"
      >
        <Outlet />
      </main>

      <BottomTabs alerts={alerts} menuOpen={menuOpen} onMore={toggleMenu} moreRef={moreRef} />
    </div>
  );
}
