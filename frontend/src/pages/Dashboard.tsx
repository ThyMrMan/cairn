import { useQuery } from "@tanstack/react-query";

import { EmptyState, Logo, Stat } from "../components/ui";
import { endpoints } from "../lib/api";
import { useAuth } from "../lib/auth";
import { bytes, relative } from "../lib/format";

export default function Dashboard() {
  const { user } = useAuth();
  const health = useQuery({ queryKey: ["health"], queryFn: endpoints.health });
  const storage = useQuery({ queryKey: ["storage"], queryFn: endpoints.storage });

  return (
    <div className="space-y-8">
      <header>
        <h1 className="text-2xl font-semibold">Dashboard</h1>
        <p className="mt-1 text-sm text-muted">
          Signed in as {user?.username}
          {user?.last_login_at && ` · last login ${relative(user.last_login_at)}`}
        </p>
      </header>

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <Stat label="Sites" value="0" sub="none archived yet" />
        <Stat label="Captures" value="0" />
        <Stat label="Archive size" value={bytes(storage.data?.archives_bytes ?? 0)} />
        <Stat
          label="Free space"
          value={bytes(storage.data?.free_bytes)}
          sub={storage.data ? `of ${bytes(storage.data.total_bytes)}` : undefined}
        />
      </div>

      <EmptyState
        icon={<Logo className="h-10 w-10" />}
        title="No sites yet"
        action={
          <button className="btn-primary" disabled title="Arrives in M1">
            Add a site
          </button>
        }
      >
        Adding sites arrives in M1, together with discovery and the wget capture engine.
        The foundation — accounts, sessions, database and packaging — is in place.
      </EmptyState>

      <section className="card p-5">
        <h2 className="text-sm font-medium">System</h2>
        <dl className="mt-3 grid gap-x-8 gap-y-2 text-sm sm:grid-cols-2">
          <Row label="Version" value={health.data?.version ?? "—"} />
          <Row label="Database" value={health.data?.db ? "connected" : "unavailable"} />
          <Row label="Data directory" value={storage.data?.data_dir ?? "—"} mono />
          <Row label="Status" value={health.data?.status ?? "—"} />
        </dl>
      </section>
    </div>
  );
}

function Row({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="flex justify-between gap-4 border-b border-border py-1.5 last:border-0">
      <dt className="text-muted">{label}</dt>
      <dd className={`truncate text-fg ${mono ? "font-mono text-xs" : ""}`} title={value}>
        {value}
      </dd>
    </div>
  );
}
