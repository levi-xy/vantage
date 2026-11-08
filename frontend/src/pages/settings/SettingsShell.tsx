// Settings area (admin only): a section nav next to the section's own page. A column on
// desktop, a select on phones.

import { NavLink, Outlet, useLocation, useNavigate } from "react-router";
import { input } from "../../components/styles";

const SECTIONS = [
  { to: "/settings/time", label: "Time" },
  { to: "/settings/users", label: "Users" },
  { to: "/settings/audit", label: "Audit log" },
  { to: "/settings/channels", label: "Notification channels" },
] as const;

export function SettingsShell() {
  const navigate = useNavigate();
  const { pathname } = useLocation();
  const current = SECTIONS.find((s) => pathname === s.to)?.to ?? "";
  return (
    <div className="md:grid md:grid-cols-[14rem_minmax(0,1fr)] md:gap-8">
      <nav aria-label="Settings sections" className="hidden md:block">
        <ul className="space-y-1">
          {SECTIONS.map((s) => (
            <li key={s.to}>
              <NavLink
                to={s.to}
                className={({ isActive }) =>
                  `block rounded-lg px-3 py-2 text-sm ${
                    isActive ? "bg-chip font-medium text-ink" : "text-muted hover:text-ink"
                  }`
                }
              >
                {s.label}
              </NavLink>
            </li>
          ))}
        </ul>
      </nav>
      <div className="mb-4 md:hidden">
        <label htmlFor="settings-section" className="sr-only">
          Settings section
        </label>
        <select
          id="settings-section"
          className={input}
          value={current}
          onChange={(e) => void navigate(e.target.value)}
        >
          {current === "" && <option value="">Settings</option>}
          {SECTIONS.map((s) => (
            <option key={s.to} value={s.to}>
              {s.label}
            </option>
          ))}
        </select>
      </div>
      <div className="min-w-0">
        <Outlet />
      </div>
    </div>
  );
}
