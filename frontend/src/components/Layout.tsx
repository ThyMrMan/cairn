import { NavLink, Outlet } from "react-router-dom";

import { useAuth } from "../lib/auth";
import { useTheme } from "../lib/theme";
import { Logo } from "./ui";

const NAV = [
  { to: "/", label: "Dashboard", end: true },
  // Enabled as the milestones land; shown disabled so the shape of the app is
  // visible from day one rather than appearing piecemeal.
  { to: "/sites", label: "Sites", soon: "M1" },
  { to: "/folders", label: "Folders", soon: "M4" },
  { to: "/profiles", label: "Access profiles", soon: "M1" },
  { to: "/jobs", label: "Jobs", soon: "M1" },
  { to: "/settings", label: "Settings" },
];

export default function Layout() {
  const { user, logout } = useAuth();
  const [theme, toggleTheme] = useTheme();

  return (
    <div className="flex min-h-full">
      <aside className="hidden w-60 shrink-0 flex-col border-r border-border bg-surface md:flex">
        <div className="flex items-center gap-2.5 px-5 py-5">
          <Logo className="h-6 w-6 text-accent" />
          <span className="text-lg font-semibold tracking-tight">Cairn</span>
        </div>

        <nav className="flex-1 space-y-0.5 px-3">
          {NAV.map((item) =>
            item.soon ? (
              <span
                key={item.to}
                className="flex cursor-not-allowed items-center justify-between rounded-md
                           px-3 py-2 text-sm text-muted opacity-60"
                title={`Arrives in ${item.soon}`}
              >
                {item.label}
                <span className="rounded bg-raised px-1.5 py-0.5 text-[10px] font-medium">
                  {item.soon}
                </span>
              </span>
            ) : (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.end}
                className={({ isActive }) =>
                  `block rounded-md px-3 py-2 text-sm transition-colors ${
                    isActive
                      ? "bg-raised font-medium text-fg"
                      : "text-muted hover:bg-raised hover:text-fg"
                  }`
                }
              >
                {item.label}
              </NavLink>
            ),
          )}
        </nav>

        <div className="space-y-2 border-t border-border p-3">
          <button onClick={toggleTheme} className="btn-ghost w-full justify-start text-muted">
            {theme === "dark" ? "Light theme" : "Dark theme"}
          </button>
          <div className="flex items-center justify-between px-1">
            <span className="truncate text-sm text-muted">{user?.username}</span>
            <button onClick={() => void logout()} className="text-sm text-muted underline">
              Sign out
            </button>
          </div>
        </div>
      </aside>

      <main className="min-w-0 flex-1 overflow-y-auto">
        <div className="mx-auto max-w-6xl px-6 py-8">
          <Outlet />
        </div>
      </main>
    </div>
  );
}
