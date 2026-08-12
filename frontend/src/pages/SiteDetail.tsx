import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";

import { DomainPicker } from "../components/DomainPicker";
import { Changes } from "../components/Changes";
import { EnginePicker } from "../components/EnginePicker";
import { Feeds } from "../components/Feeds";
import { LiveLog } from "../components/LiveLog";
import { Replay } from "../components/Replay";
import { Alert, EmptyState, Spinner } from "../components/ui";
import { ApiError, endpoints } from "../lib/api";
import { bytes, dateTime, relative } from "../lib/format";
import { StatusPill } from "./Sites";

export default function SiteDetail() {
  const { id } = useParams();
  const siteId = Number(id);
  const client = useQueryClient();
  const navigate = useNavigate();
  const [watching, setWatching] = useState<number | null>(null);
  // Set by a search result linking to the page it matched.
  const [params] = useSearchParams();
  const replayUrl = params.get("replay") ?? undefined;
  const replayTimestamp = params.get("ts");
  // A search result can ask for the reader directly: "which of my archives
  // mentioned this" is nearly always followed by wanting to read it.
  const replayMode: "rendered" | "reader" =
    params.get("mode") === "reader" ? "reader" : "rendered";

  const site = useQuery({
    queryKey: ["site", siteId],
    queryFn: () => endpoints.site(siteId),
    enabled: Number.isFinite(siteId),
  });
  const captures = useQuery({
    queryKey: ["captures", siteId],
    queryFn: () => endpoints.captures(siteId),
    enabled: Number.isFinite(siteId),
  });

  // Reattach to a capture that is already running — after a page reload, or
  // when it was started from another tab.
  useEffect(() => {
    if (site.data?.running_job_id) setWatching(site.data.running_job_id);
  }, [site.data?.running_job_id]);

  const start = useMutation({
    mutationFn: (kind: string) => endpoints.startCapture(siteId, kind),
    onSuccess: (result) => setWatching(result.job_id),
  });

  const cancel = useMutation({
    mutationFn: (jobId: number) => endpoints.cancelJob(jobId),
  });

  const remove = useMutation({
    mutationFn: () => endpoints.deleteSite(siteId),
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey: ["sites"] });
      navigate("/sites");
    },
  });

  if (site.isLoading) return <Spinner className="h-5 w-5 text-muted" />;
  if (site.error || !site.data) {
    return <Alert kind="error">That site could not be loaded.</Alert>;
  }

  const data = site.data;
  const scopeNotes = data.scope.notes ?? [];

  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <Link to="/sites" className="text-sm text-muted hover:underline">
            ← Sites
          </Link>
          <div className="mt-1 flex items-center gap-2">
            <h1 className="truncate text-2xl font-semibold">{data.title}</h1>
            <StatusPill status={data.status} />
          </div>
          <a
            href={data.seed_url}
            target="_blank"
            rel="noreferrer noopener"
            className="text-sm text-muted hover:underline"
          >
            {data.seed_url}
          </a>
        </div>

        <div className="flex gap-2">
          <button
            className="btn-primary"
            disabled={start.isPending || watching !== null}
            onClick={() => start.mutate("full")}
          >
            {start.isPending && <Spinner />}
            {watching !== null ? "Capture running" : "Capture now"}
          </button>
          {watching !== null && (
            <button className="btn-ghost" onClick={() => cancel.mutate(watching)}>
              Cancel
            </button>
          )}
        </div>
      </header>

      {start.error && <Alert kind="error">{(start.error as ApiError).message}</Alert>}

      {watching !== null && (
        <LiveLog
          jobId={watching}
          onFinished={() => {
            setWatching(null);
            void client.invalidateQueries({ queryKey: ["captures", siteId] });
            void client.invalidateQueries({ queryKey: ["site", siteId] });
          }}
        />
      )}

      <div className="grid gap-4 sm:grid-cols-4">
        <Metric label="Captures" value={String(data.capture_count)} />
        <Metric label="URLs" value={data.url_count.toLocaleString()} />
        <Metric label="Size on disk" value={bytes(data.size_bytes)} />
        <Metric label="Last capture" value={relative(data.last_capture_at)} />
      </div>

      {scopeNotes.length > 0 && (
        <Alert kind="warn" title="Worth knowing about this scope">
          <ul className="list-disc space-y-1 pl-5">
            {scopeNotes.map((note) => (
              <li key={note}>{note}</li>
            ))}
          </ul>
        </Alert>
      )}

      <LiveSite siteId={siteId} seedUrl={data.seed_url} />

      <Scope
        siteId={siteId}
        onChanged={() => void client.invalidateQueries({ queryKey: ["site", siteId] })}
      />

      <EnginePicker site={data} />

      <Feeds siteId={siteId} />

      <ReplaySection
        siteId={siteId}
        captureCount={data.capture_count}
        initialUrl={replayUrl}
        initialTimestamp={replayTimestamp}
        initialMode={replayMode}
      />

      <Changes siteId={siteId} captureCount={data.capture_count} />

      <Exports siteId={siteId} captures={captures.data ?? []} />

      <section className="space-y-3">
        <h2 className="text-sm font-medium">Captures</h2>
        {captures.data && captures.data.length > 0 ? (
          <div className="grid gap-2">
            {captures.data.map((capture) => (
              <CaptureRow key={capture.id} captureId={capture.id} />
            ))}
          </div>
        ) : (
          <EmptyState title="Not captured yet">
            Press <strong>Capture now</strong> to crawl this site to WARC.
          </EmptyState>
        )}
      </section>

      <section className="card space-y-3 p-5">
        <h2 className="text-sm font-medium">Details</h2>
        <dl className="grid gap-x-8 gap-y-2 text-sm sm:grid-cols-2">
          <Row label="Engine" value={`${data.engine_id}`} />
          <Row label="Archive path" value={data.archive_path} mono />
          <Row label="Access profile" value={data.profile_id ? `#${data.profile_id}` : "none"} />
          <Row label="Created" value={dateTime(data.created_at)} />
        </dl>
        <div className="pt-2">
          <button
            className="btn-ghost text-danger"
            onClick={() => {
              if (confirm(`Move "${data.title}" to trash? Its archives go with it.`)) {
                remove.mutate();
              }
            }}
          >
            Delete site
          </button>
          {remove.error && (
            <p className="mt-2 text-sm text-danger">{(remove.error as ApiError).message}</p>
          )}
        </div>
      </section>
    </div>
  );
}

/**
 * Whether the site this archive is of still exists.
 *
 * A single line, and normally the boring one. It earns its place on the day it
 * says the blog now returns 404 — which is the moment the archive stopped
 * being a copy and became the only copy.
 */
function LiveSite({ siteId, seedUrl }: { siteId: number; seedUrl: string }) {
  const client = useQueryClient();
  const health = useQuery({
    queryKey: ["site-health", siteId],
    queryFn: () => endpoints.siteHealth(),
    select: (all) => all.problems.find((p) => p.site_id === siteId) ?? null,
  });
  const check = useMutation({
    mutationFn: () => endpoints.checkSiteHealth(siteId),
    onSuccess: () => client.invalidateQueries({ queryKey: ["site-health"] }),
  });

  const problem = health.data;
  const result = check.data;
  if (!problem && !result) {
    return (
      <p className="hint">
        <button
          className="hover:underline"
          onClick={() => check.mutate()}
          disabled={check.isPending}
        >
          {check.isPending ? "Checking…" : "Check whether the live site is still there"}
        </button>
      </p>
    );
  }

  const state = problem?.state ?? result?.state ?? "live";
  const tone = state === "gone" ? "warn" : state === "moved" ? "info" : "info";
  return (
    <Alert
      kind={tone}
      title={
        state === "gone"
          ? "The live site is gone"
          : state === "moved"
            ? "The live site has moved"
            : "The live site answers normally"
      }
    >
      <p>
        {result?.message ??
          (problem?.state === "moved"
            ? `${seedUrl} now redirects to ${problem.final_url}. Add that address as a second seed to keep archiving it.`
            : `${seedUrl} returns ${problem?.http_status ?? "an error"}${
                problem?.since ? `, and has since ${dateTime(problem.since)}` : ""
              }.`)}
      </p>
      <button
        className="btn-ghost mt-2 text-xs"
        onClick={() => check.mutate()}
        disabled={check.isPending}
      >
        {check.isPending && <Spinner />}
        Check again
      </button>
    </Alert>
  );
}

function ReplaySection({
  siteId,
  captureCount,
  initialUrl,
  initialTimestamp,
  initialMode = "rendered",
}: {
  siteId: number;
  captureCount: number;
  initialUrl?: string;
  initialTimestamp?: string | null;
  initialMode?: "rendered" | "reader";
}) {
  // Collapsed until there is something to see: an iframe that loads pywb on
  // every visit to a site nobody has captured yet is pure noise. Open anyway
  // when a search result asked for a particular page — arriving at a link and
  // having to hunt for the panel it opens is not arriving at a link.
  const [open, setOpen] = useState(captureCount > 0 || Boolean(initialUrl));

  return (
    <section className="card p-5">
      <button
        className="flex w-full items-center justify-between text-left"
        onClick={() => setOpen((v) => !v)}
      >
        <div>
          <h2 className="text-sm font-medium">Browse the archive</h2>
          <p className="hint mt-0.5">The site as it was, served from the WARCs.</p>
        </div>
        <span className="text-xs text-muted">{open ? "Hide" : "Show"}</span>
      </button>
      {open && (
        <div className="mt-4">
          <Replay
            siteId={siteId}
            initialUrl={initialUrl}
            initialTimestamp={initialTimestamp ?? null}
            initialMode={initialMode}
          />
        </div>
      )}
    </section>
  );
}

/**
 * WACZ exports.
 *
 * The list is a directory read, so a file copied in over the share shows up
 * here and one deleted over the share disappears — there is no table of
 * exports to fall out of step with the disk.
 */
function Exports({ siteId, captures }: { siteId: number; captures: { id: number }[] }) {
  const client = useQueryClient();
  const [open, setOpen] = useState(false);
  const [checked, setChecked] = useState<Record<string, string>>({});

  const list = useQuery({
    queryKey: ["exports", siteId],
    queryFn: () => endpoints.exports(siteId),
    enabled: open,
  });
  const refresh = () => client.invalidateQueries({ queryKey: ["exports", siteId] });

  const build = useMutation({
    mutationFn: () => endpoints.exportSite(siteId),
    onSuccess: () => setTimeout(refresh, 1500),
  });
  const remove = useMutation({
    mutationFn: (name: string) => endpoints.deleteExport(siteId, name),
    onSuccess: refresh,
  });
  const verify = useMutation({
    mutationFn: (name: string) => endpoints.verifyExport(siteId, name),
    onSuccess: (result, name) =>
      setChecked((prev) => ({
        ...prev,
        [name]: result.ok
          ? `${result.records} record(s), every one resolving`
          : result.problems.slice(0, 3).join("; "),
      })),
  });

  return (
    <section className="card p-5">
      <button
        className="flex w-full items-center justify-between text-left"
        onClick={() => setOpen((v) => !v)}
      >
        <div>
          <h2 className="text-sm font-medium">Export</h2>
          <p className="hint mt-0.5">
            One <code>.wacz</code> file holding the whole archive. It opens in replayweb.page
            with no server, and it is the format to keep an offsite copy in.
          </p>
        </div>
        <span className="text-xs text-muted">{open ? "Hide" : "Show"}</span>
      </button>

      {open && (
        <div className="mt-4 space-y-3">
          <div className="flex flex-wrap items-center gap-2">
            <button
              className="btn-ghost"
              onClick={() => build.mutate()}
              disabled={build.isPending || captures.length === 0}
            >
              {build.isPending && <Spinner />}
              Export every capture
            </button>
            {captures.length === 0 && (
              <span className="hint">Nothing to export until this site has a capture.</span>
            )}
            {build.data && <span className="hint">Queued as job #{build.data.job_id}.</span>}
            {build.error && (
              <span className="text-sm text-danger">{(build.error as ApiError).message}</span>
            )}
          </div>

          {list.data && list.data.length > 0 ? (
            <ul className="divide-y divide-border text-sm">
              {list.data.map((entry) => (
                <li key={entry.name} className="flex flex-wrap items-center gap-3 py-2">
                  <a
                    className="flex-1 break-all font-mono text-xs text-accent hover:underline"
                    href={endpoints.exportUrl(siteId, entry.name)}
                    download
                  >
                    {entry.name}
                  </a>
                  <span className="shrink-0 text-xs text-muted tabular-nums">
                    {bytes(entry.size_bytes)}
                  </span>
                  <span className="shrink-0 text-xs text-muted">{relative(entry.created_at)}</span>
                  <button
                    className="btn-ghost shrink-0 text-xs"
                    onClick={() => verify.mutate(entry.name)}
                    disabled={verify.isPending}
                  >
                    Verify
                  </button>
                  <button
                    className="btn-ghost shrink-0 text-xs text-danger"
                    onClick={() => {
                      if (confirm(`Delete ${entry.name}? The archive itself is untouched.`)) {
                        remove.mutate(entry.name);
                      }
                    }}
                  >
                    Delete
                  </button>
                  {checked[entry.name] && (
                    <span className="w-full text-xs text-muted">{checked[entry.name]}</span>
                  )}
                </li>
              ))}
            </ul>
          ) : (
            <p className="hint">No exports yet.</p>
          )}
        </div>
      )}
    </section>
  );
}

function Scope({ siteId, onChanged }: { siteId: number; onChanged?: () => void }) {
  const [open, setOpen] = useState(true);
  const scope = useQuery({
    queryKey: ["scope", siteId],
    queryFn: () => endpoints.scope(siteId),
    enabled: open,
  });

  return (
    <section className="card p-5">
      <button
        className="flex w-full items-center justify-between text-left"
        onClick={() => setOpen((v) => !v)}
      >
        <div>
          <h2 className="text-sm font-medium">Domains and crawl scope</h2>
          <p className="hint mt-0.5">Which hosts to crawl, and which to take files from.</p>
        </div>
        <span className="text-xs text-muted">{open ? "Hide" : "Show"}</span>
      </button>

      {open && (
        <div className="mt-4 space-y-4">
          <Seeds siteId={siteId} onChanged={onChanged} />

          <DomainPicker siteId={siteId} onChanged={onChanged} />

          {scope.data && scope.data.wget_preview.length > 0 && (
            <details>
              <summary className="cursor-pointer text-xs text-muted">
                Exactly what the crawler will be told
              </summary>
              <pre className="mt-2 overflow-x-auto rounded bg-raised/60 p-3 font-mono text-[11px]">
                {scope.data.wget_preview.join("\n")}
              </pre>
            </details>
          )}
        </div>
      )}
    </section>
  );
}

/**
 * Where this site starts from.
 *
 * Almost always one address, which is why this is a quiet line rather than a
 * section: the case it exists for — a blog that moved to a custom domain, or
 * one that spans two — is real and rare. Keeping both under one site is what
 * keeps one index, one replay collection, and one capture selector that knows
 * about every version of a page.
 */
function Seeds({ siteId, onChanged }: { siteId: number; onChanged?: () => void }) {
  const client = useQueryClient();
  const [draft, setDraft] = useState("");
  const seeds = useQuery({ queryKey: ["seeds", siteId], queryFn: () => endpoints.seeds(siteId) });

  const refresh = async () => {
    await Promise.all([
      client.invalidateQueries({ queryKey: ["seeds", siteId] }),
      client.invalidateQueries({ queryKey: ["scope", siteId] }),
      client.invalidateQueries({ queryKey: ["site", siteId] }),
    ]);
    onChanged?.();
  };

  const add = useMutation({
    mutationFn: () => endpoints.addSeed(siteId, draft.trim()),
    onSuccess: async () => {
      setDraft("");
      await refresh();
    },
  });
  const drop = useMutation({
    mutationFn: (url: string) => endpoints.removeSeed(siteId, url),
    onSuccess: refresh,
  });

  const list = seeds.data?.seeds ?? [];
  const primary = seeds.data?.primary ?? "";

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-baseline gap-2">
        <h3 className="text-xs font-medium uppercase text-muted">Starting points</h3>
        {list.length > 1 && (
          <span className="hint">
            {list.length} seeds, one scope, one archive
          </span>
        )}
      </div>

      <ul className="space-y-1 text-sm">
        {list.map((url) => (
          <li key={url} className="flex items-center gap-2">
            <span className="min-w-0 flex-1 truncate font-mono text-xs">{url}</span>
            {url === primary ? (
              <span className="text-xs text-muted">primary</span>
            ) : (
              <button
                className="btn-ghost px-2 py-0.5 text-xs"
                onClick={() => drop.mutate(url)}
                disabled={drop.isPending}
              >
                Remove
              </button>
            )}
          </li>
        ))}
      </ul>

      <form
        className="flex flex-wrap gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          if (draft.trim()) add.mutate();
        }}
      >
        <input
          className="field min-w-0 flex-1 font-mono text-xs"
          placeholder="Another address this site lives at"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          spellCheck={false}
          aria-label="Additional seed URL"
        />
        <button className="btn-ghost shrink-0 text-xs" disabled={add.isPending || !draft.trim()}>
          {add.isPending && <Spinner />}
          Add seed
        </button>
      </form>

      {add.error && <Alert kind="error">{(add.error as ApiError).message}</Alert>}
      {drop.error && <Alert kind="error">{(drop.error as ApiError).message}</Alert>}
      {add.data?.note && <p className="hint">{add.data.note}</p>}
    </div>
  );
}

function CaptureRow({ captureId }: { captureId: number }) {
  const [open, setOpen] = useState(false);
  const capture = useQuery({
    queryKey: ["capture", captureId],
    queryFn: () => endpoints.capture(captureId),
  });

  if (!capture.data) return null;
  const data = capture.data;
  const warnings = ((data.manifest?.stats as Record<string, unknown>)?.warnings ??
    []) as string[];

  return (
    <div className="card overflow-hidden">
      <button
        className="flex w-full flex-wrap items-center gap-4 p-4 text-left hover:bg-raised"
        onClick={() => setOpen((v) => !v)}
      >
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="font-mono text-xs">{data.dir_name}</span>
            <StatusPill status={data.status} />
          </div>
          <p className="mt-0.5 text-xs text-muted">
            {dateTime(data.started_at)} · {data.kind}
          </p>
        </div>
        <dl className="flex shrink-0 gap-6 text-right text-xs">
          <div>
            <dt className="text-muted">URLs</dt>
            <dd className="tabular-nums">{data.url_count.toLocaleString()}</dd>
          </div>
          <div>
            <dt className="text-muted">Errors</dt>
            <dd className={`tabular-nums ${data.error_count ? "text-warn" : ""}`}>
              {data.error_count}
            </dd>
          </div>
          <div>
            <dt className="text-muted">Size</dt>
            <dd className="tabular-nums">{bytes(data.bytes_written)}</dd>
          </div>
        </dl>
      </button>

      {open && (
        <div className="space-y-4 border-t border-border p-4">
          {warnings.length > 0 && (
            <Alert kind="warn" title="This capture has gaps worth knowing about">
              <ul className="list-disc space-y-1 pl-5">
                {warnings.map((warning) => (
                  <li key={warning}>{warning}</li>
                ))}
              </ul>
            </Alert>
          )}

          <div>
            <h3 className="mb-1.5 text-xs font-medium uppercase text-muted">Files</h3>
            <ul className="space-y-1 font-mono text-[11px]">
              {data.artifacts.map((artifact) => (
                <li key={artifact.name} className="flex justify-between gap-4">
                  <span className="truncate">{artifact.name}</span>
                  <span className="shrink-0 text-muted">{bytes(artifact.size)}</span>
                </li>
              ))}
            </ul>
          </div>

          <CaptureUrls captureId={captureId} errorCount={data.error_count} />
          <CaptureLog captureId={captureId} />
        </div>
      )}
    </div>
  );
}

function CaptureUrls({ captureId, errorCount }: { captureId: number; errorCount: number }) {
  const [errorsOnly, setErrorsOnly] = useState(errorCount > 0);
  const urls = useQuery({
    queryKey: ["capture-urls", captureId, errorsOnly],
    queryFn: () =>
      endpoints.captureUrls(captureId, { errors_only: errorsOnly ? "true" : undefined, per_page: 100 }),
  });

  return (
    <div>
      <div className="mb-1.5 flex items-center justify-between">
        <h3 className="text-xs font-medium uppercase text-muted">
          URLs {urls.data ? `(${urls.data.total.toLocaleString()})` : ""}
        </h3>
        <label className="flex items-center gap-1.5 text-xs text-muted">
          <input
            type="checkbox"
            checked={errorsOnly}
            onChange={(event) => setErrorsOnly(event.target.checked)}
          />
          Errors only
        </label>
      </div>

      <div className="max-h-60 overflow-y-auto rounded border border-border">
        <table className="w-full text-[11px]">
          <tbody>
            {urls.data?.items.map((row) => (
              <tr key={row.id} className="border-b border-border last:border-0">
                <td
                  className={`w-12 px-2 py-1 tabular-nums ${
                    row.error || (row.status_code ?? 0) >= 400 ? "text-danger" : "text-muted"
                  }`}
                >
                  {row.status_code ?? "ERR"}
                </td>
                <td className="truncate px-2 py-1 font-mono">{row.url}</td>
                <td className="w-24 truncate px-2 py-1 text-right text-muted">
                  {row.error ?? row.mime ?? ""}
                </td>
              </tr>
            ))}
            {urls.data?.items.length === 0 && (
              <tr>
                <td className="px-2 py-3 text-center text-muted">
                  {errorsOnly ? "No errors — everything was fetched." : "No URLs recorded."}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function CaptureLog({ captureId }: { captureId: number }) {
  const [open, setOpen] = useState(false);
  const log = useQuery({
    queryKey: ["capture-log", captureId],
    queryFn: () => endpoints.captureLog(captureId),
    enabled: open,
  });

  return (
    <details onToggle={(event) => setOpen(event.currentTarget.open)}>
      <summary className="cursor-pointer text-xs text-muted">Crawl log</summary>
      <pre className="mt-2 max-h-60 overflow-auto rounded bg-raised/60 p-3 font-mono text-[11px]">
        {log.data ?? "Loading…"}
      </pre>
    </details>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="card p-4">
      <p className="text-xs font-medium uppercase tracking-wide text-muted">{label}</p>
      <p className="mt-1 text-xl font-semibold tabular-nums">{value}</p>
    </div>
  );
}

function Row({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="flex justify-between gap-4 border-b border-border py-1.5 last:border-0">
      <dt className="text-muted">{label}</dt>
      <dd className={`truncate ${mono ? "font-mono text-xs" : ""}`} title={value}>
        {value}
      </dd>
    </div>
  );
}
