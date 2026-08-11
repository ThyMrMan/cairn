import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { DomainPicker } from "../components/DomainPicker";
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

      <Scope
        siteId={siteId}
        onChanged={() => void client.invalidateQueries({ queryKey: ["site", siteId] })}
      />

      <ReplaySection siteId={siteId} captureCount={data.capture_count} />

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

function ReplaySection({ siteId, captureCount }: { siteId: number; captureCount: number }) {
  // Collapsed until there is something to see: an iframe that loads pywb on
  // every visit to a site nobody has captured yet is pure noise.
  const [open, setOpen] = useState(captureCount > 0);

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
          <Replay siteId={siteId} />
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
