import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import type { ReactNode, RefObject } from "react";
import { Link, NavLink, Outlet, useLocation } from "react-router";
import type { Role } from "../api";
import { OPEN_ALERTS_LIMIT, useOpenAlerts } from "../cards/openAlerts";
import { ThemeToggle } from "../components/ThemeToggle";
import { systemAlertCause, systemAlertText } from "./health";
import { useDeliveryStatus } from "./deliveryStatus";
import { HealthStatus } from "./HealthStatus";
import { useLastDataUpdate, useLiveUpdates } from "./live";
import { useNavOrder } from "./navOrder";
import { useApiClient, useSession, useUser } from "./session";
import { commitsMismatch, FRONTEND_COMMIT, FRONTEND_VERSION, shownVersion } from "./version";
import { setTimeZone } from "./time";
import { TIME_KEY } from "./timeSettings";

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
          {systemAlertCause(alert.system_kind, alert.reason) && (
            <>{systemAlertCause(alert.system_kind, alert.reason)} </>
          )}
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
  {
    to: "/map",
    label: "Map",
    end: false,
    icon: (
      <Icon>
        <circle cx="6" cy="6" r="2.5" />
        <circle cx="18" cy="8" r="2.5" />
        <circle cx="12" cy="18" r="2.5" />
        <path d="M8 7l8 1M7.5 8l3 8M16.5 10l-3 6" />
      </Icon>
    ),
  },
];

/** Admins also get Settings (time, users, audit log, notification channels). */
const SETTINGS_TAB: Tab = {
  to: "/settings",
  label: "Settings",
  end: false,
  icon: (
    <Icon>
      <path d="M4 7h10M18 7h2M4 17h2M10 17h10" />
      <circle cx="16" cy="7" r="2" />
      <circle cx="8" cy="17" r="2" />
    </Icon>
  ),
};

function useTabs(): readonly Tab[] {
  return useUser().role === "admin" ? [...TABS, SETTINGS_TAB] : TABS;
}

interface MenuItem {
  to: string;
  label: string;
  /** Only users with this role see the link. The route and the backend check it too. */
  role?: Role;
}

const MENU: readonly MenuItem[] = [
  { to: "/account", label: "My account" },
  { to: "/settings/about", label: "About" },
];

/** The Dashboard tab also stands for every other dashboard (`/d/<id>`). */
function useOnOtherDashboard(): boolean {
  return useLocation().pathname.startsWith("/d/");
}

/**
 * Desktop: the tabs down the left sidebar, the current one highlighted. Icons only when
 * collapsed. Drag an item to move it, or focus it and press Alt+Up / Alt+Down.
 */
function SideNav({
  tabs,
  moveTo,
  alerts,
  collapsed,
}: {
  tabs: readonly Tab[];
  moveTo: (id: string, targetId: string) => void;
  alerts: number;
  collapsed: boolean;
}) {
  const otherDashboard = useOnOtherDashboard();
  const navRef = useRef<HTMLElement>(null);
  const dragging = useRef<string | null>(null);
  const refocus = useRef<string | null>(null);
  // A moved link may lose focus when the browser re-inserts it: put it back.
  const order = tabs.map((t) => t.to).join("\n");
  useLayoutEffect(() => {
    const id = refocus.current;
    refocus.current = null;
    if (id === null) return;
    const links = navRef.current?.querySelectorAll<HTMLElement>("a[data-nav-id]") ?? [];
    for (const link of links) if (link.dataset.navId === id) link.focus();
  }, [order]);
  return (
    <nav ref={navRef} aria-label="Main" className="hidden gap-1 px-3 py-2 md:flex md:flex-col">
      {tabs.map((tab, index) => (
        <NavLink
          key={tab.to}
          to={tab.to}
          end={tab.end}
          data-nav-id={tab.to}
          draggable
          title={collapsed ? tab.label : undefined}
          onDragStart={(event) => {
            dragging.current = tab.to;
            event.dataTransfer.effectAllowed = "move";
            // Only our own id travels: no URL for the browser to open on a stray drop.
            event.dataTransfer.clearData();
            event.dataTransfer.setData("text/plain", tab.to);
          }}
          onDragOver={(event) => {
            if (dragging.current !== null) event.preventDefault();
          }}
          onDrop={(event) => {
            event.preventDefault();
            const id = dragging.current;
            dragging.current = null;
            if (id !== null) moveTo(id, tab.to);
          }}
          onDragEnd={() => {
            dragging.current = null;
          }}
          onKeyDown={(event) => {
            if (!event.altKey || (event.key !== "ArrowUp" && event.key !== "ArrowDown")) return;
            event.preventDefault();
            const target = tabs[index + (event.key === "ArrowUp" ? -1 : 1)];
            if (!target) return;
            refocus.current = tab.to;
            moveTo(tab.to, target.to);
          }}
          className={({ isActive }) =>
            `relative flex h-10 items-center gap-3 rounded-lg ${collapsed ? "justify-center" : "px-3"} ${focusRing} ${
              isActive || (tab.to === "/" && otherDashboard)
                ? "bg-chip font-medium text-ink"
                : "text-muted hover:bg-chip hover:text-ink"
            }`
          }
        >
          {tab.icon}
          <span className={collapsed ? "sr-only" : ""}>{tab.label}</span>
          {tab.to === "/alerts" && (
            <AlertBadge
              count={alerts}
              className={
                collapsed
                  ? "absolute top-0.5 right-0.5 px-[5px] text-[10px] leading-[15px]"
                  : "ml-auto"
              }
            />
          )}
        </NavLink>
      ))}
    </nav>
  );
}

const SIDEBAR_KEY = "vantage.sidebarCollapsed";

/** Whether the desktop sidebar is collapsed to icons; remembered per browser (a convenience only). */
function useSidebarCollapsed(): [boolean, () => void] {
  const [collapsed, setCollapsed] = useState(() => {
    try {
      return window.localStorage.getItem(SIDEBAR_KEY) === "1";
    } catch {
      return false;
    }
  });
  const toggle = () => {
    const next = !collapsed;
    setCollapsed(next);
    try {
      window.localStorage.setItem(SIDEBAR_KEY, next ? "1" : "0");
    } catch {
      // Storage blocked: the choice just isn't remembered.
    }
  };
  return [collapsed, toggle];
}

/** Map pages use the whole area next to the sidebar. */
function useFullBleed(): boolean {
  const path = useLocation().pathname;
  return path === "/map" || path.startsWith("/map/");
}

/** Phone: a bottom tab bar. "More" opens the account menu. */
function BottomTabs({
  tabs,
  alerts,
  menuOpen,
  onMore,
  moreRef,
}: {
  tabs: readonly Tab[];
  alerts: number;
  menuOpen: boolean;
  onMore: () => void;
  moreRef: RefObject<HTMLButtonElement>;
}) {
  const otherDashboard = useOnOtherDashboard();
  const tabClass = (active: boolean) =>
    `relative flex flex-col items-center justify-center gap-[3px] text-[11px] ${focusRing} ${
      active ? "text-in" : "text-muted"
    }`;
  return (
    <nav
      aria-label="Tabs"
      className={`fixed inset-x-0 bottom-0 z-30 grid h-16 border-t border-line bg-panel md:hidden ${
        tabs.length > TABS.length ? "grid-cols-6" : "grid-cols-5"
      }`}
    >
      {tabs.map((tab) => (
        <NavLink
          key={tab.to}
          to={tab.to}
          end={tab.end}
          className={({ isActive }) => tabClass(isActive || (tab.to === "/" && otherDashboard))}
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
      className="fixed inset-x-0 bottom-16 z-40 border-t border-line bg-panel py-2 shadow-lg md:absolute md:bottom-0 md:left-full md:max-h-[calc(100vh-2rem)] md:ml-3 md:w-56 md:overflow-y-auto md:rounded-lg md:border"
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

/** "Vantage <release>" at the foot of the sidebar; a marker while web app and server are different builds. */
function VersionFooter() {
  const user = useUser();
  const mismatch = commitsMismatch(FRONTEND_COMMIT, user.server_commit);
  return (
    <Link
      to="/settings/about"
      className={`hidden truncate px-1 text-center text-xs text-muted hover:text-ink md:block ${focusRing}`}
    >
      Vantage {shownVersion(user.server_version, FRONTEND_VERSION)}
      {mismatch && (
        <span className="text-crit-text" title="The web app and the server are different builds">
          {" "}
          <span aria-hidden="true">⚠</span>
          <span className="sr-only"> (version mismatch, see About)</span>
        </span>
      )}
    </Link>
  );
}

/** Navigation around every page: a left sidebar on desktop; a top bar and bottom tab bar on phones. */
export function Layout() {
  const [menuOpen, setMenuOpen] = useState(false);
  const user = useUser();
  const queryClient = useQueryClient();
  const client = useApiClient();
  // A 401 here reaches the session's unauthorized listener, which shows the login page.
  const checkSession = useCallback(() => {
    client.me().catch(() => undefined);
  }, [client]);
  // Every time on screen is in the admin-chosen timezone. Set before the page renders; the
  // page remounts when the zone changes, so nothing keeps showing the old one.
  const timeSettings = useQuery({
    queryKey: TIME_KEY,
    queryFn: () => client.timeSettings(),
    staleTime: 60_000,
  });
  const timeZone = timeSettings.data?.timezone;
  setTimeZone(timeZone);
  const live = useLiveUpdates(queryClient, checkSession);
  const lastUpdate = useLastDataUpdate(queryClient);
  const alerts = useOpenAlertCount();
  const panelRef = useRef<HTMLDivElement>(null);
  const userButtonRef = useRef<HTMLButtonElement>(null);
  const moreRef = useRef<HTMLButtonElement>(null);
  const toggleMenu = () => setMenuOpen((open) => !open);
  const [collapsed, toggleCollapsed] = useSidebarCollapsed();
  const fullBleed = useFullBleed();
  const { items: tabs, moveTo } = useNavOrder(useTabs());

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

  const userButton = (
    <button
      ref={userButtonRef}
      type="button"
      aria-expanded={menuOpen}
      aria-controls="account-menu"
      onClick={toggleMenu}
      title={collapsed ? user.username : undefined}
      className={`hidden h-9 items-center gap-2 rounded-lg border border-line text-ink hover:bg-chip md:flex ${
        collapsed ? "justify-center" : "pr-2.5 pl-1.5"
      } ${focusRing}`}
    >
      <span
        aria-hidden="true"
        className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-chip text-[11px] font-semibold uppercase"
      >
        {user.username.slice(0, 1)}
      </span>
      <span className={collapsed ? "sr-only" : "min-w-0 flex-1 truncate text-left"}>
        {user.username}
      </span>
      <span className="sr-only"> ({ROLE_LABELS[user.role]})</span>
      {!collapsed && (
        <svg
          width="12"
          height="12"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          aria-hidden="true"
        >
          <path d="M9 6l6 6-6 6" />
        </svg>
      )}
    </button>
  );

  return (
    <div className="min-h-screen bg-canvas text-ink md:flex">
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:absolute focus:top-2 focus:left-2 focus:z-50 focus:rounded focus:bg-panel focus:px-3 focus:py-2"
      >
        Skip to content
      </a>
      {/* One header: a top bar on phones, a left sidebar (collapsible to icons) on desktop. */}
      <header
        className={`sticky top-0 z-30 flex h-14 items-center gap-4 border-b border-line bg-panel px-4 md:h-screen md:shrink-0 md:flex-col md:items-stretch md:gap-0 md:border-r md:border-b-0 md:px-0 ${
          collapsed ? "md:w-16" : "md:w-60"
        }`}
      >
        <Link
          to="/"
          className={`flex items-center gap-2.5 text-[17px] font-semibold tracking-[-0.01em] md:h-[60px] md:shrink-0 ${
            collapsed ? "md:justify-center" : "md:px-5"
          } ${focusRing}`}
        >
          <Logo />
          <span className={collapsed ? "md:sr-only" : ""}>Vantage</span>
        </Link>
        <SideNav tabs={tabs} moveTo={moveTo} alerts={alerts} collapsed={collapsed} />
        <div className="relative ml-auto flex items-center gap-3 md:mt-auto md:ml-0 md:flex-col md:items-stretch md:gap-2 md:border-t md:border-line md:p-3">
          <HealthStatus connection={live} lastUpdate={lastUpdate} compact={collapsed} />
          {userButton}
          <div className={`flex items-center gap-2 ${collapsed ? "md:flex-col" : ""}`}>
            <ThemeToggle />
            <button
              type="button"
              onClick={toggleCollapsed}
              aria-label={collapsed ? "Show sidebar" : "Hide sidebar"}
              aria-expanded={!collapsed}
              title={collapsed ? "Show sidebar" : "Hide sidebar"}
              className={`hidden h-9 w-9 shrink-0 items-center justify-center rounded-lg border border-line text-muted hover:bg-chip hover:text-ink md:inline-flex ${focusRing}`}
            >
              <svg
                width="16"
                height="16"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
                strokeLinejoin="round"
                aria-hidden="true"
              >
                <rect x="3" y="4" width="18" height="16" rx="2" />
                <path d="M9 4v16" />
                <path d={collapsed ? "M13 10l2 2-2 2" : "M16 10l-2 2 2 2"} />
              </svg>
            </button>
          </div>
          {!collapsed && <VersionFooter />}
          {menuOpen && <AccountMenu onClose={() => setMenuOpen(false)} panelRef={panelRef} />}
        </div>
      </header>

      <div className={`flex min-w-0 flex-1 flex-col ${fullBleed ? "md:h-screen" : ""}`}>
        <DeliveryBanner />
        <main
          id="main"
          className={
            fullBleed
              ? "w-full min-w-0 px-4 pt-4 pb-24 md:flex md:min-h-0 md:flex-1 md:flex-col md:p-0"
              : "mx-auto w-full max-w-[1440px] min-w-0 px-4 pt-4 pb-24 md:px-8 md:pt-7 md:pb-12"
          }
        >
          <Outlet key={timeZone ?? "UTC"} />
        </main>
      </div>

      <BottomTabs
        tabs={tabs}
        alerts={alerts}
        menuOpen={menuOpen}
        onMore={toggleMenu}
        moreRef={moreRef}
      />
    </div>
  );
}
