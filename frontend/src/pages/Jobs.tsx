import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";

import { Alert, EmptyState, Spinner } from "../components/ui";
import { type Job, endpoints } from "../lib/api";
import { bytes, dateTime, relative } from "../lib/format";
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

  const cancel = useMutation({
    mutationFn: (id: number) => endpoints.cancelJob(id),
    onSuccess: () => client.invalidateQueries({ queryKey: ["jobs"] }),
  });

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-2xl font-semibold">Jobs</h1>
        <p className="mt-1 text-sm text-muted">Captures, past and present.</p>
      </header>

      {jobs.isLoading ? (
        <Spinner className="h-5 w-5 text-muted" />
      ) : jobs.data && jobs.data.items.length > 0 ? (
        <div className="grid gap-2">
          {jobs.data.items.map((job) => (
            <JobRow key={job.id} job={job} onCancel={() => cancel.mutate(job.id)} />
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

function JobRow({ job, onCancel }: { job: Job; onCancel: () => void }) {
  const active = ACTIVE.has(job.status);

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
            {job.finished_at
              ? `finished ${relative(job.finished_at)}`
              : job.started_at
                ? `started ${relative(job.started_at)}`
                : `queued ${dateTime(job.queued_at)}`}
            {job.progress?.done != null && ` · ${job.progress.done.toLocaleString()} URLs`}
            {job.progress?.bytes != null && ` · ${bytes(job.progress.bytes)}`}
          </p>
        </div>

        {active && (
          <button className="btn-ghost text-xs" onClick={onCancel}>
            Cancel
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
