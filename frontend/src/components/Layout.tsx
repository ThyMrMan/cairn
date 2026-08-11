import { NavLink, Outlet } from "react-router-dom";

import { useAuth } from "../lib/auth";
import { useTheme } from "../lib/theme";
import { useVersion } from "../lib/version";
import { Logo } from "./ui";

// Every destination is live as of M4; the "arrives in Mx" placeholder this
// list used to carry is gone with the last of them.
const NAV = [
  { to: "/", label: "Dashboard", end: true },
  { to: "/search", label: "Search" },
  { to: "/sites", label: "Sites" },
  { to: "/folders", label: "Folders" },
  { to: "/profiles", label: "Access profiles" },
  { to: "/jobs", label: "Jobs" },
  { to: "/settings", label: "Settings" },
];

export default function Layout() {
  const { user, logout } = useAuth();
  const [theme, toggleTheme] = useTheme();
  const version = useVersion();

  return (
    <div className="flex min-h-full">
      <aside className="hidden w-60 shrink-0 flex-col border-r border-border bg-surface md:flex">
        <div className="flex items-center gap-2.5 px-5 py-5">
          <Logo className="h-6 w-6 text-accent" />
          <span className="text-lg font-semibold tracking-tight">Cairn</span>
        </div>

        <nav className="flex-1 space-y-0.5 px-3">
          {NAV.map((item) => (
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
          ))}
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
          {/* Which build is running. The version alone never changes between
              commits, so the build id after it is the part that answers
              "is this the one I just deployed?". */}
          <p
            className="truncate px-1 font-mono text-[11px] leading-none text-muted opacity-70"
            title={
              version.data?.built_at
                ? `built ${version.data.built_at}`
                : "built from a source checkout"
            }
          >
            {version.data ? `v${version.data.label}` : " "}
          </p>
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
