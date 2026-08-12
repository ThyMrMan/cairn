import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";

import { EmptyState, Logo, Stat } from "../components/ui";
import { endpoints } from "../lib/api";
import { useAuth } from "../lib/auth";
import { bytes, relative } from "../lib/format";

export default function Dashboard() {
  const { user } = useAuth();
  const health = useQuery({ queryKey: ["health"], queryFn: endpoints.health });
  const storage = useQuery({ queryKey: ["storage"], queryFn: endpoints.storage });
  const sites = useQuery({
    queryKey: ["sites", ""],
    queryFn: () => endpoints.sites({ sort: "-last_capture_at" }),
  });
  const activeJobs = useQuery({
    queryKey: ["jobs", "active"],
    queryFn: () => endpoints.jobs({ active: "true", per_page: 1 }),
    refetchInterval: 5000,
  });

  // /api/storage reports the whole data directory; the per-site rollup is the
  // honest number for "how much of that is archives".
  const archiveBytes = (sites.data?.items ?? []).reduce((sum, s) => sum + s.size_bytes, 0);

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
        <Stat
          label="Sites"
          value={String(sites.data?.total ?? 0)}
          sub={sites.data?.total ? undefined : "none archived yet"}
        />
        <Stat
          label="Active jobs"
          value={String(activeJobs.data?.total ?? 0)}
          sub={activeJobs.data?.total ? "running now" : undefined}
        />
        <Stat label="Archive size" value={bytes(archiveBytes)} />
        <Stat
          label="Free space"
          value={bytes(storage.data?.free_bytes)}
          sub={storage.data ? `of ${bytes(storage.data.total_bytes)}` : undefined}
        />
      </div>

      {(sites.data?.total ?? 0) > 0 && <NeedsAttention />}

      {sites.data && sites.data.items.length > 0 ? (
        <section className="space-y-3">
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-medium">Recent sites</h2>
            <Link to="/sites" className="text-sm text-muted hover:underline">
              All sites →
            </Link>
          </div>
          <div className="grid gap-2">
            {sites.data.items.slice(0, 5).map((site) => (
              <Link
                key={site.id}
                to={`/sites/${site.id}`}
                className="card flex items-center justify-between gap-4 p-3.5 hover:bg-raised"
              >
                <div className="min-w-0">
                  <p className="truncate text-sm font-medium">{site.title}</p>
                  <p className="truncate text-xs text-muted">{site.primary_host}</p>
                </div>
                <span className="shrink-0 text-xs text-muted">
                  {site.last_capture_at ? relative(site.last_capture_at) : "never captured"}
                </span>
              </Link>
            ))}
          </div>
        </section>
      ) : (
        <EmptyState
          icon={<Logo className="h-10 w-10" />}
          title="No sites yet"
          action={
            <Link to="/sites" className="btn-primary">
              Add a site
            </Link>
          }
        >
          Point Cairn at a URL and it crawls that host to WARC. Discovery and the domain
          picker arrive in M2; replay in the browser in M3.
        </EmptyState>
      )}

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

/**
 * The periodic report, on the page rather than only in a notification.
 *
 * It answers the question no other view does: what has *not* happened. A feed
 * that stopped returning entries, a site nothing has captured in six weeks and
 * a profile whose cookies expire on Tuesday are all invisible everywhere else,
 * because nothing failed — things merely stopped.
 *
 * Silent when there is nothing to say. A panel that says "all good" every day
 * is a panel people stop reading, and then it is not there on the day it says
 * something else.
 */
function NeedsAttention() {
  const digest = useQuery({ queryKey: ["digest"], queryFn: () => endpoints.digest(30) });
  const data = digest.data;
  if (!data || !data.has_problems) return null;

  const rows: { key: string; text: string; to?: string }[] = [
    ...data.quiet_sites.map((s) => ({
      key: `quiet-${s.site_id}`,
      text: `${s.title} — nothing captured in ${s.days} days`,
      to: `/sites/${s.site_id}`,
    })),
    ...data.stalled_feeds.map((f) => ({
      key: `feed-${f.feed_id}`,
      text: `${f.site} — a feed is being polled but has returned nothing`,
      to: `/sites/${f.site_id}`,
    })),
    ...data.failed_jobs.map((j) => ({
      key: `job-${j.job_id}`,
      text: `${j.type} failed${j.site ? ` for ${j.site}` : ""}${j.error ? `: ${j.error}` : ""}`,
      to: "/jobs",
    })),
    ...data.vanished_sites.map((s) => ({
      key: `gone-${s.site_id}`,
      text:
        s.state === "moved"
          ? `${s.title} — the live site now redirects to ${s.final_url}`
          : `${s.title} — the live site returns ${s.http_status ?? 404}. These pages are now only in your archive.`,
      to: `/sites/${s.site_id}`,
    })),
    ...data.expiring_profiles.map((p) => ({
      key: `profile-${p.profile_id}`,
      text: `${p.name} — credentials ${p.expired ? "have expired" : "expire soon"}`,
      to: "/profiles",
    })),
    ...(data.integrity.findings
      ? [
          {
            key: "integrity",
            text: `Integrity check found ${data.integrity.findings} problem(s)`,
            to: "/settings",
          },
        ]
      : []),
  ];

  return (
    <section className="space-y-3">
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-medium">Needs attention</h2>
        <span className="text-xs text-muted">last {data.days} days</span>
      </div>
      <ul className="card divide-y divide-border">
        {rows.slice(0, 8).map((row) => (
          <li key={row.key} className="px-3.5 py-2.5 text-sm">
            {row.to ? (
              <Link to={row.to} className="hover:underline">
                {row.text}
              </Link>
            ) : (
              row.text
            )}
          </li>
        ))}
      </ul>
    </section>
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
