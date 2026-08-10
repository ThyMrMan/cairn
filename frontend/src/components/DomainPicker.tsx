import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";

import {
  type ApiError,
  type DiscoveredHost,
  type DiscoverySummary,
  type HostRule,
  endpoints,
} from "../lib/api";
import { bytes } from "../lib/format";
import { Alert, EmptyState, Spinner } from "./ui";

/**
 * The domain picker.
 *
 * Two independent checkboxes per host is the whole point: you want an image
 * CDN's pictures without treating it as a site to crawl. Everything unknown
 * starts off, so a crawl can never wander onto a blog that merely appeared in
 * a sidebar.
 */
export function DomainPicker({ siteId, onChanged }: { siteId: number; onChanged?: () => void }) {
  const client = useQueryClient();
  const [draft, setDraft] = useState<Record<string, HostRule> | null>(null);
  const [rejects, setRejects] = useState<string[] | null>(null);
  const [saved, setSaved] = useState(false);

  const discovery = useQuery({
    queryKey: ["discovery", siteId],
    queryFn: () => endpoints.discovery(siteId),
  });
  const scope = useQuery({ queryKey: ["scope", siteId], queryFn: () => endpoints.scope(siteId) });

  // The draft starts from what is saved, so an un-edited picker shows exactly
  // the current scope rather than a fresh set of defaults.
  useEffect(() => {
    if (!discovery.data?.hosts || !scope.data || draft) return;
    const initial: Record<string, HostRule> = {};
    for (const host of discovery.data.hosts) {
      initial[host.host] = {
        host: host.host,
        crawl_pages: host.crawl_pages,
        fetch_assets: host.fetch_assets,
        path_prefix: null,
        allow_extensionless: host.allow_extensionless,
      };
    }
    setDraft(initial);
    setRejects(scope.data.reject_patterns);
  }, [discovery.data, scope.data, draft]);

  const save = useMutation({
    mutationFn: () =>
      endpoints.putScope(siteId, {
        hosts: Object.values(draft ?? {}),
        reject_patterns: rejects ?? [],
        obey_robots: scope.data?.obey_robots ?? true,
        max_pages: scope.data?.max_pages ?? null,
        max_bytes: scope.data?.max_bytes ?? null,
        politeness: scope.data?.politeness ?? {},
      }),
    onSuccess: async () => {
      setSaved(true);
      await Promise.all([
        client.invalidateQueries({ queryKey: ["scope", siteId] }),
        client.invalidateQueries({ queryKey: ["site", siteId] }),
        client.invalidateQueries({ queryKey: ["preview", siteId] }),
      ]);
      onChanged?.();
    },
  });

  const preset = discovery.data?.discovery?.summary.fingerprint.preset ?? null;
  const applyPreset = useMutation({
    mutationFn: () => endpoints.applyPreset(siteId, preset?.id ?? ""),
    onSuccess: async () => {
      setDraft(null);
      await client.invalidateQueries({ queryKey: ["discovery", siteId] });
      await client.invalidateQueries({ queryKey: ["scope", siteId] });
      onChanged?.();
    },
  });

  const dirty = useMemo(() => {
    if (!draft || !discovery.data) return false;
    return discovery.data.hosts.some(
      (h) =>
        draft[h.host]?.crawl_pages !== h.crawl_pages ||
        draft[h.host]?.fetch_assets !== h.fetch_assets,
    );
  }, [draft, discovery.data]);

  if (discovery.isLoading) return <Spinner className="h-5 w-5 text-muted" />;

  if (!discovery.data?.discovery) {
    return <NotYetDiscovered siteId={siteId} onDone={onChanged} />;
  }

  const summary = discovery.data.discovery.summary;
  const hosts = discovery.data.hosts;
  // A host with no draft entry yet still has to end up a complete rule, or a
  // partial object reaches the API and the scope silently loses its host name.
  const blank = (host: string): HostRule => ({
    host,
    crawl_pages: false,
    fetch_assets: false,
    path_prefix: null,
    allow_extensionless: false,
  });

  const set = (host: string, patch: Partial<HostRule>) => {
    setSaved(false);
    setDraft((prev) =>
      prev ? { ...prev, [host]: { ...(prev[host] ?? blank(host)), ...patch } } : prev,
    );
  };

  const bulk = (match: (h: DiscoveredHost) => boolean, patch: Partial<HostRule>) => {
    setSaved(false);
    setDraft((prev) => {
      if (!prev) return prev;
      const next = { ...prev };
      for (const host of hosts.filter(match)) {
        next[host.host] = { ...(next[host.host] ?? blank(host.host)), ...patch };
      }
      return next;
    });
  };

  return (
    <div className="space-y-4">
      <Found summary={summary} />

      {summary.warnings?.map((warning) => (
        <Alert kind="warn" key={warning}>
          {warning}
        </Alert>
      ))}

      {(discovery.data.diff?.new_hosts?.length ?? 0) > 0 && (
        <Alert kind="info" title="New since the last run">
          {discovery.data.diff?.new_hosts.join(", ")}
        </Alert>
      )}

      <div className="flex flex-wrap items-center gap-2 text-xs">
        <span className="text-muted">Select:</span>
        <button
          className="btn-ghost px-2 py-1 text-xs"
          onClick={() => bulk((h) => h.role === "images" || h.role === "cdn", { fetch_assets: true })}
        >
          all images and CDNs
        </button>
        <button
          className="btn-ghost px-2 py-1 text-xs"
          onClick={() =>
            bulk((h) => h.role === "analytics" || h.role === "ads", {
              fetch_assets: false,
              crawl_pages: false,
            })
          }
        >
          no analytics
        </button>
        <button
          className="btn-ghost px-2 py-1 text-xs"
          onClick={() => bulk((h) => !h.is_seed_host, { fetch_assets: false, crawl_pages: false })}
        >
          nothing but the site itself
        </button>
        {preset && (
          <button
            className="btn-ghost px-2 py-1 text-xs"
            onClick={() => applyPreset.mutate()}
            disabled={applyPreset.isPending}
          >
            {applyPreset.isPending && <Spinner />}
            apply the {preset.name} preset
          </button>
        )}
      </div>

      <div className="card overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="border-b border-border text-left text-xs text-muted">
            <tr>
              <th className="px-3 py-2 font-medium">Host</th>
              <th className="px-3 py-2 text-right font-medium">Links</th>
              <th className="px-3 py-2 text-right font-medium">Assets</th>
              <th className="px-3 py-2 font-medium">Role</th>
              <th className="px-3 py-2 text-center font-medium">
                Crawl
                <span className="block font-normal normal-case">follow its pages</span>
              </th>
              <th className="px-3 py-2 text-center font-medium">
                Assets
                <span className="block font-normal normal-case">fetch its files</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {hosts.map((host) => (
              <HostRow
                key={host.host}
                host={host}
                rule={draft?.[host.host]}
                onChange={(patch) => set(host.host, patch)}
              />
            ))}
          </tbody>
        </table>
      </div>

      <RejectPatterns
        patterns={rejects ?? []}
        onChange={(next) => {
          setSaved(false);
          setRejects(next);
        }}
      />

      {save.error && <Alert kind="error">{(save.error as ApiError).message}</Alert>}

      <div className="flex items-center gap-3">
        <button className="btn-primary" onClick={() => save.mutate()} disabled={save.isPending}>
          {save.isPending && <Spinner />}
          Save scope
        </button>
        {dirty && !saved && <span className="text-xs text-warn">Unsaved changes</span>}
        {saved && <span className="text-xs text-ok">Saved</span>}
        <Rediscover siteId={siteId} onDone={() => setDraft(null)} />
      </div>

      <Preview siteId={siteId} />
    </div>
  );
}

function HostRow({
  host,
  rule,
  onChange,
}: {
  host: DiscoveredHost;
  rule: HostRule | undefined;
  onChange: (patch: Partial<HostRule>) => void;
}) {
  const [open, setOpen] = useState(false);
  const roleTone =
    { self: "text-accent", analytics: "text-danger", images: "text-ok", cdn: "text-ok" }[
      host.role
    ] ?? "text-muted";

  return (
    <>
      <tr className="border-b border-border last:border-0">
        <td className="px-3 py-2">
          <button className="text-left font-mono text-xs hover:underline" onClick={() => setOpen((v) => !v)}>
            {host.host}
          </button>
          {host.is_seed_host && (
            <span className="ml-2 rounded bg-accent/15 px-1.5 py-0.5 text-[10px] text-accent">
              this site
            </span>
          )}
          <span className="block text-[11px] text-muted">{host.registrable}</span>
        </td>
        <td className="px-3 py-2 text-right tabular-nums">{host.link_refs.toLocaleString()}</td>
        <td className="px-3 py-2 text-right tabular-nums">{host.asset_refs.toLocaleString()}</td>
        <td className={`px-3 py-2 text-xs ${roleTone}`}>{host.role}</td>
        <td className="px-3 py-2 text-center">
          <input
            type="checkbox"
            checked={rule?.crawl_pages ?? false}
            onChange={(e) => onChange({ crawl_pages: e.target.checked })}
            aria-label={`Crawl pages on ${host.host}`}
          />
        </td>
        <td className="px-3 py-2 text-center">
          <input
            type="checkbox"
            checked={rule?.fetch_assets ?? false}
            onChange={(e) => onChange({ fetch_assets: e.target.checked })}
            aria-label={`Fetch assets from ${host.host}`}
          />
        </td>
      </tr>
      {open && (
        <tr className="border-b border-border bg-raised/40">
          <td colSpan={6} className="px-3 py-2">
            {rule?.fetch_assets && !rule?.crawl_pages && (
              <label className="mb-2 flex items-center gap-2 text-xs">
                <input
                  type="checkbox"
                  checked={rule?.allow_extensionless ?? false}
                  onChange={(e) => onChange({ allow_extensionless: e.target.checked })}
                />
                Allow URLs with no file extension
                <span className="text-muted">
                  — needed for image proxies like <code>…/blogger_img_proxy/AEn0k_…</code>, at the
                  cost of possibly fetching a page from this host.
                </span>
              </label>
            )}
            <p className="mb-1 text-[11px] uppercase text-muted">Sample URLs</p>
            <ul className="space-y-0.5 font-mono text-[11px]">
              {host.sample_urls.length ? (
                host.sample_urls.map((url) => (
                  <li key={url} className="truncate text-muted">
                    {url}
                  </li>
                ))
              ) : (
                <li className="text-muted">none recorded</li>
              )}
            </ul>
          </td>
        </tr>
      )}
    </>
  );
}

function RejectPatterns({
  patterns,
  onChange,
}: {
  patterns: string[];
  onChange: (next: string[]) => void;
}) {
  const [value, setValue] = useState("");
  return (
    <div className="card p-4">
      <h3 className="text-sm font-medium">Skip URLs matching</h3>
      <p className="hint mt-0.5">
        Regular expressions. Blogger's <code>[?&amp;]m=1</code> is the important one — it serves
        every post twice and rejecting the duplicate halves the crawl with no content loss.
      </p>
      <ul className="mt-3 space-y-1">
        {patterns.map((pattern) => (
          <li key={pattern} className="flex items-center justify-between gap-3">
            <code className="truncate text-xs">{pattern}</code>
            <button
              className="text-xs text-muted hover:text-danger"
              onClick={() => onChange(patterns.filter((p) => p !== pattern))}
            >
              remove
            </button>
          </li>
        ))}
        {patterns.length === 0 && <li className="text-xs text-muted">No patterns.</li>}
      </ul>
      <form
        className="mt-3 flex gap-2"
        onSubmit={(event) => {
          event.preventDefault();
          const trimmed = value.trim();
          if (trimmed && !patterns.includes(trimmed)) onChange([...patterns, trimmed]);
          setValue("");
        }}
      >
        <input
          className="field text-xs"
          placeholder="[?&amp;]m=1"
          value={value}
          onChange={(event) => setValue(event.target.value)}
        />
        <button className="btn-ghost text-xs">Add</button>
      </form>
    </div>
  );
}

function Preview({ siteId }: { siteId: number }) {
  const preview = useQuery({
    queryKey: ["preview", siteId],
    queryFn: () => endpoints.scopePreview(siteId),
  });
  if (!preview.data) return null;
  const data = preview.data;

  return (
    <div className="card p-4">
      <h3 className="text-sm font-medium">If you captured now</h3>
      <dl className="mt-2 grid gap-x-8 gap-y-1 text-sm sm:grid-cols-2">
        <Row label="Pages to crawl" value={`~${data.pages_to_crawl.toLocaleString()}`} />
        <Row label="Asset hosts allowed" value={String(data.asset_hosts.length)} />
        <Row
          label="Excluded by pattern"
          value={data.excluded_by_pattern.toLocaleString()}
        />
        <Row label="Rough size" value={bytes(data.estimated_bytes)} />
      </dl>
      {data.notes.map((note) => (
        <p key={note} className="hint mt-2">
          {note}
        </p>
      ))}
    </div>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex justify-between gap-4 border-b border-border py-1 last:border-0">
      <dt className="text-muted">{label}</dt>
      <dd className="tabular-nums">{value}</dd>
    </div>
  );
}

function Found({ summary }: { summary: DiscoverySummary }) {
  const fp = summary.fingerprint;
  return (
    <div className="flex flex-wrap gap-x-6 gap-y-1 text-xs text-muted">
      <span>
        Platform:{" "}
        <strong className="text-fg">
          {fp.preset?.name ?? fp.platform}
          {fp.confidence === "weak" && " (probably)"}
        </strong>
      </span>
      <span>
        {summary.urls_from_sitemaps.toLocaleString()} URLs from {summary.sitemaps.length} sitemap(s)
      </span>
      <span>
        {summary.urls_from_feeds.toLocaleString()} from {summary.feeds.length} feed(s)
      </span>
      <span>{summary.pages_fetched} pages sampled</span>
    </div>
  );
}

function NotYetDiscovered({ siteId, onDone }: { siteId: number; onDone?: () => void }) {
  return (
    <EmptyState
      title="Not indexed yet"
      action={<Rediscover siteId={siteId} onDone={onDone} label="Index this site" primary />}
    >
      Indexing reads the sitemap, the feeds and a sample of pages to work out which domains this
      site pulls from. It writes nothing and can be re-run any time.
    </EmptyState>
  );
}

export function Rediscover({
  siteId,
  onDone,
  label = "Re-index",
  primary = false,
}: {
  siteId: number;
  onDone?: () => void;
  label?: string;
  primary?: boolean;
}) {
  const client = useQueryClient();
  const [jobId, setJobId] = useState<number | null>(null);

  const start = useMutation({
    mutationFn: () => endpoints.discover(siteId),
    onSuccess: (result) => setJobId(result.job_id),
  });

  // Discovery is short, so a poll is simpler than a stream and just as timely.
  useQuery({
    queryKey: ["discovery-job", jobId],
    queryFn: async () => {
      const job = await endpoints.job(jobId as number);
      if (job.status !== "queued" && job.status !== "running") {
        setJobId(null);
        await client.invalidateQueries({ queryKey: ["discovery", siteId] });
        await client.invalidateQueries({ queryKey: ["scope", siteId] });
        await client.invalidateQueries({ queryKey: ["site", siteId] });
        onDone?.();
      }
      return job;
    },
    enabled: jobId !== null,
    refetchInterval: 1000,
  });

  const running = jobId !== null || start.isPending;
  return (
    <button
      className={primary ? "btn-primary" : "btn-ghost text-xs"}
      onClick={() => start.mutate()}
      disabled={running}
    >
      {running && <Spinner />}
      {running ? "Indexing…" : label}
    </button>
  );
}
