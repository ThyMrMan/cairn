import { useState } from "react";
import type { ReactNode } from "react";

export function Logo({ className = "h-7 w-7" }: { className?: string }) {
  // A cairn: stacked stones marking a trail.
  return (
    <svg viewBox="0 0 24 24" className={className} aria-hidden="true" fill="none">
      <ellipse cx="12" cy="20" rx="7.5" ry="2.2" fill="currentColor" opacity="0.9" />
      <ellipse cx="12" cy="15.4" rx="5.8" ry="2" fill="currentColor" opacity="0.72" />
      <ellipse cx="12" cy="11.2" rx="4.2" ry="1.8" fill="currentColor" opacity="0.55" />
      <ellipse cx="12" cy="7.6" rx="2.8" ry="1.5" fill="currentColor" opacity="0.4" />
      <ellipse cx="12" cy="4.8" rx="1.6" ry="1.1" fill="currentColor" opacity="0.28" />
    </svg>
  );
}

export function Spinner({ className = "h-4 w-4" }: { className?: string }) {
  return (
    <svg className={`animate-spin ${className}`} viewBox="0 0 24 24" aria-hidden="true">
      <circle cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="3" fill="none"
              opacity="0.25" />
      <path d="M12 2a10 10 0 0 1 10 10" stroke="currentColor" strokeWidth="3" fill="none"
            strokeLinecap="round" />
    </svg>
  );
}

export function Alert({
  kind = "error",
  title,
  children,
}: {
  kind?: "error" | "warn" | "ok" | "info";
  title?: string;
  children: ReactNode;
}) {
  const tone = {
    error: "border-danger/40 bg-danger/10 text-danger",
    warn: "border-warn/40 bg-warn/10 text-warn",
    ok: "border-ok/40 bg-ok/10 text-ok",
    info: "border-border bg-raised text-fg",
  }[kind];

  return (
    <div className={`rounded-md border px-3 py-2.5 text-sm ${tone}`} role="alert">
      {title && <p className="font-medium">{title}</p>}
      <div className={title ? "mt-1 opacity-90" : ""}>{children}</div>
    </div>
  );
}

export function Field({
  label,
  hint,
  children,
  htmlFor,
}: {
  label: string;
  hint?: string;
  children: ReactNode;
  htmlFor?: string;
}) {
  return (
    <div>
      <label className="label" htmlFor={htmlFor}>
        {label}
      </label>
      {children}
      {hint && <p className="hint mt-1.5">{hint}</p>}
    </div>
  );
}

export function EmptyState({
  icon,
  title,
  children,
  action,
}: {
  icon?: ReactNode;
  title: string;
  children?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="flex flex-col items-center justify-center rounded-lg border border-dashed
                    border-border px-6 py-14 text-center">
      {icon && <div className="mb-3 text-muted">{icon}</div>}
      <h3 className="text-base font-medium text-fg">{title}</h3>
      {children && <p className="mt-1.5 max-w-md text-sm text-muted">{children}</p>}
      {action && <div className="mt-5">{action}</div>}
    </div>
  );
}

export function Stat({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="card p-4">
      <p className="text-xs font-medium uppercase tracking-wide text-muted">{label}</p>
      <p className="mt-1.5 text-2xl font-semibold tabular-nums text-fg">{value}</p>
      {sub && <p className="hint mt-0.5">{sub}</p>}
    </div>
  );
}

/**
 * A panel's open/closed state, remembered across page loads.
 *
 * Without the remembering this is a toy: collapsing a section you never use
 * only helps if it is still collapsed the next time you open the page, and
 * per-render state resets on every navigation.
 *
 * Kept in localStorage rather than on the site, because it is a preference of
 * whoever is looking rather than a property of the archive — two people using
 * one instance should not fight over each other's layout.
 */
export function useCollapsible(key: string, defaultOpen: boolean) {
  const storageKey = `cairn.panel.${key}`;
  const [open, setOpen] = useState(() => {
    try {
      const saved = localStorage.getItem(storageKey);
      return saved === null ? defaultOpen : saved === "1";
    } catch {
      // Private mode, or storage disabled. A remembered panel is a nicety;
      // losing the whole page because of it is not a trade worth making.
      return defaultOpen;
    }
  });

  const toggle = () =>
    setOpen((wasOpen) => {
      const next = !wasOpen;
      try {
        localStorage.setItem(storageKey, next ? "1" : "0");
      } catch {
        /* see above */
      }
      return next;
    });

  return { open, toggle };
}

/**
 * The clickable header of a collapsible card, matching the one the replay and
 * export panels already use: title and hint on the left, Hide/Show on the
 * right, the whole row a button.
 *
 * `extra` is for a control that must sit in the header without being swallowed
 * by it — Feeds has an "Add a feed" button there, and nesting one button
 * inside another is invalid and un-clickable.
 */
export function PanelHeader({
  title,
  hint,
  open,
  onToggle,
  extra,
}: {
  title: string;
  hint?: string;
  open: boolean;
  onToggle: () => void;
  extra?: ReactNode;
}) {
  return (
    <div className="flex flex-wrap items-start justify-between gap-3">
      <button className="flex flex-1 items-start justify-between text-left" onClick={onToggle}>
        <div>
          <h2 className="text-sm font-medium">{title}</h2>
          {hint && <p className="hint mt-0.5">{hint}</p>}
        </div>
        <span className="ml-3 shrink-0 text-xs text-muted">{open ? "Hide" : "Show"}</span>
      </button>
      {open && extra}
    </div>
  );
}
