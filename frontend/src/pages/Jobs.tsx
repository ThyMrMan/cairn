import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";

import { Alert, EmptyState, Spinner } from "../components/ui";
import { type Job, endpoints } from "../lib/api";
import { bytes, dateTime, ranFromTo, relative } from "../lib/format";
import { StatusPill } from "./Sites";

const ACTIVE = new Set(["queued", "running"]);

export default function Jobs() {
  const client = useQueryClient();
  const jobs = useQuery({
    queryKey: ["jobs"],
    queryFn: () => endpoints.jobs({ per_page: 50 }),
    // Cheap poll: the per-job SSE stream carries the detail, this list only
    // needs to notice that something started or finished.
    refetchInterval: 3000,
  });

  const refresh = () => client.invalidateQueries({ queryKey: ["jobs"] });

  const cancel = useMutation({
    mutationFn: (id: number) => endpoints.cancelJob(id),
    onSuccess: refresh,
  });

  const remove = useMutation({
    mutationFn: (id: number) => endpoints.deleteJob(id),
    onSuccess: refresh,
  });

  const clear = useMutation({
    mutationFn: (status?: string) => endpoints.clearJobs(status ? { status } : {}),
    onSuccess: refresh,
  });

  const items = jobs.data?.items ?? [];
  const failed = items.filter((job) => job.status === "failed").length;
  const finished = items.filter((job) => !ACTIVE.has(job.status)).length;

  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold">Jobs</h1>
          <p className="mt-1 text-sm text-muted">Captures, past and present.</p>
        </div>
        {/*
          Clearing is offered by what is actually on the page. "Clear failed"
          when nothing failed is a button that does nothing, and the count is
          what makes a destructive action safe to press without a dialog.
        */}
        {finished > 0 && (
          <div className="flex gap-2">
            {failed > 0 && (
              <button
                className="btn-ghost text-xs"
                disabled={clear.isPending}
                onClick={() => clear.mutate("failed")}
              >
                {clear.isPending && <Spinner />}
                Clear {failed} failed
              </button>
            )}
            <button
              className="btn-ghost text-xs"
              disabled={clear.isPending}
              onClick={() => {
                if (confirm(`Delete ${finished} finished job(s)? Captures are not affected.`))
                  clear.mutate(undefined);
              }}
            >
              Clear finished
            </button>
          </div>
        )}
      </header>

      {clear.error && <Alert kind="error">{(clear.error as Error).message}</Alert>}
      {remove.error && <Alert kind="error">{(remove.error as Error).message}</Alert>}

      {jobs.isLoading ? (
        <Spinner className="h-5 w-5 text-muted" />
      ) : items.length > 0 ? (
        <div className="grid gap-2">
          {items.map((job) => (
            <JobRow
              key={job.id}
              job={job}
              onCancel={() => cancel.mutate(job.id)}
              onDelete={() => remove.mutate(job.id)}
            />
          ))}
        </div>
      ) : (
        <EmptyState title="Nothing has run yet">
          Starting a capture from a site's page queues a job here.
        </EmptyState>
      )}
    </div>
  );
}

function JobRow({
  job,
  onCancel,
  onDelete,
}: {
  job: Job;
  onCancel: () => void;
  onDelete: () => void;
}) {
  const active = ACTIVE.has(job.status);
  const [confirming, setConfirming] = useState(false);

  return (
    <div className="card p-4">
      <div className="flex flex-wrap items-center gap-4">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="text-sm font-medium">
              {job.site_id ? (
                <Link to={`/sites/${job.site_id}`} className="hover:underline">
                  {job.site_title ?? `Site ${job.site_id}`}
                </Link>
              ) : (
                job.type
              )}
            </span>
            <StatusPill status={job.status} />
            <span className="text-xs text-muted">#{job.id}</span>
          </div>
          <p className="mt-0.5 text-xs text-muted">
            {/*
              Wall-clock rather than only "3h ago". Relative time answers "is
              this recent?" and nothing else, and the question about a finished
              capture is usually how long it took and when it ran.
            */}
            {job.started_at ? ranFromTo(job.started_at, job.finished_at) : null}
            {!job.started_at && `queued ${dateTime(job.queued_at)}`}
            {job.finished_at && ` · ${relative(job.finished_at)}`}
            {job.progress?.done != null &&
              ` · ${job.progress.done.toLocaleString()} ${job.progress.unit ?? "URLs"}`}
            {job.progress?.bytes != null && ` · ${bytes(job.progress.bytes)}`}
          </p>
        </div>

        {active ? (
          <button className="btn-ghost text-xs" onClick={onCancel}>
            Cancel
          </button>
        ) : confirming ? (
          // Inline rather than a dialog: one row is a small enough action that
          // a modal would be heavier than the thing it guards, and this still
          // means no single click deletes anything.
          <div className="flex items-center gap-2">
            <span className="text-xs text-muted">Delete this job?</span>
            <button className="btn-ghost text-xs text-danger" onClick={onDelete}>
              Delete
            </button>
            <button className="btn-ghost text-xs" onClick={() => setConfirming(false)}>
              Keep
            </button>
          </div>
        ) : (
          <button className="btn-ghost text-xs" onClick={() => setConfirming(true)}>
            Dismiss
          </button>
        )}
      </div>

      {job.error && (
        <div className="mt-3">
          <Alert kind="error" title="This job failed">
            <pre className="overflow-x-auto whitespace-pre-wrap font-mono text-[11px]">
              {job.error}
            </pre>
          </Alert>
        </div>
      )}
    </div>
  );
}
