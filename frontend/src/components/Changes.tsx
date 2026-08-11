import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { ApiError, endpoints, type BlockChange, type RetentionPlan } from "../lib/api";
import { bytes, dateTime, relative } from "../lib/format";
import { Alert, EmptyState, Spinner } from "./ui";

/**
 * What changed between captures, and what may therefore be deleted.
 *
 * The two sit together because they answer one question between them: is
 * another full capture worth its disk? The diff says what the last one
 * changed; retention says what could go if the answer is "not much".
 */
export function Changes({ siteId, captureCount }: { siteId: number; captureCount: number }) {
  const [open, setOpen] = useState(false);

  return (
    <section className="card p-5">
      <button
        className="flex w-full items-center justify-between text-left"
        onClick={() => setOpen((v) => !v)}
      >
        <div>
          <h2 className="text-sm font-medium">Changes and retention</h2>
          <p className="hint mt-0.5">
            What the last recapture actually changed, and which captures could be deleted
            without losing anything that exists only here.
          </p>
        </div>
        <span className="text-xs text-muted">{open ? "Hide" : "Show"}</span>
      </button>

      {open && (
        <div className="mt-4 space-y-6">
          {captureCount >= 2 ? (
            <Diff siteId={siteId} />
          ) : (
            <EmptyState title="Nothing to compare yet">
              Comparing needs two finished captures of this site.
            </EmptyState>
          )}
          <Retention siteId={siteId} />
        </div>
      )}
    </section>
  );
}

function Diff({ siteId }: { siteId: number }) {
  const [page, setPage] = useState<string | null>(null);

  const diff = useQuery({
    queryKey: ["diff", siteId],
    queryFn: () => endpoints.diffCaptures(siteId),
  });
  const resources = useQuery({
    queryKey: ["diff-resources", siteId],
    queryFn: () => endpoints.diffResources(siteId),
  });

  if (diff.isLoading) return <Spinner className="h-5 w-5 text-muted" />;
  if (diff.error) {
    return <Alert kind="warn">{(diff.error as ApiError).message}</Alert>;
  }
  if (!diff.data) return null;

  const data = diff.data;
  const assets = resources.data?.resources ?? [];

  return (
    <div className="space-y-3">
      <h3 className="text-xs font-medium uppercase tracking-wide text-muted">
        {data.before_capture} → {data.after_capture}
      </h3>

      {data.note && <p className="hint">{data.note}</p>}

      <div className="flex flex-wrap gap-4 text-sm">
        <Count label="changed" value={data.changed} accent={data.changed > 0} />
        <Count label="added" value={data.added} accent={data.added > 0} />
        <Count label="removed" value={data.removed} accent={data.removed > 0} />
        <Count label="unchanged" value={data.unchanged} />
        {assets.length > 0 && <Count label="assets differing" value={assets.length} />}
      </div>

      {data.pages.length === 0 ? (
        <p className="text-sm text-muted">
          Nothing changed. A full recapture of this site rewrites {data.unchanged} identical
          page(s) — an incremental capture would have cost a fraction of it.
        </p>
      ) : (
        <ul className="divide-y divide-border text-sm">
          {data.pages.slice(0, 50).map((entry) => (
            <li key={entry.url} className="flex flex-wrap items-center gap-3 py-2">
              <span
                className={`shrink-0 rounded px-1.5 py-0.5 text-[11px] ${
                  entry.kind === "added"
                    ? "bg-ok/15 text-ok"
                    : entry.kind === "removed"
                      ? "bg-danger/15 text-danger"
                      : "bg-raised text-muted"
                }`}
              >
                {entry.kind}
              </span>
              <span className="min-w-0 flex-1 truncate" title={entry.url}>
                {entry.title || entry.url}
              </span>
              {entry.kind === "changed" && (
                <button
                  className="btn-ghost shrink-0 text-xs"
                  onClick={() => setPage(page === entry.url ? null : entry.url)}
                >
                  {page === entry.url ? "Hide" : "What changed"}
                </button>
              )}
            </li>
          ))}
        </ul>
      )}

      {page && <PageDiffView siteId={siteId} url={page} />}

      {assets.length > 0 && (
        <details className="text-sm">
          <summary className="cursor-pointer text-muted">
            {assets.length} asset(s) differ
          </summary>
          <ul className="mt-2 space-y-1">
            {assets.slice(0, 30).map((asset) => (
              <li key={`${asset.kind}:${asset.url}`} className="break-all text-xs">
                <span className="text-muted">{asset.kind}</span> {asset.url}
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}

function Count({ label, value, accent }: { label: string; value: number; accent?: boolean }) {
  return (
    <span className={accent ? "font-medium" : "text-muted"}>
      <span className="tabular-nums">{value.toLocaleString()}</span> {label}
    </span>
  );
}

function PageDiffView({ siteId, url }: { siteId: number; url: string }) {
  const diff = useQuery({
    queryKey: ["diff-page", siteId, url],
    queryFn: () => endpoints.diffPage(siteId, { url }),
  });

  if (diff.isLoading) return <Spinner className="h-4 w-4 text-muted" />;
  if (!diff.data) return null;

  return (
    <div className="space-y-2 rounded-md border border-border p-3">
      <p className="break-all text-xs text-muted">{url}</p>
      {diff.data.note && <p className="hint">{diff.data.note}</p>}
      {diff.data.blocks.slice(0, 40).map((block, i) => (
        <Block key={i} block={block} />
      ))}
    </div>
  );
}

function Block({ block }: { block: BlockChange }) {
  if (block.kind === "added") {
    return <p className="rounded bg-ok/10 px-2 py-1 text-sm">+ {block.after}</p>;
  }
  if (block.kind === "removed") {
    return <p className="rounded bg-danger/10 px-2 py-1 text-sm line-through">− {block.before}</p>;
  }
  return (
    <div className="space-y-1 text-sm">
      {block.words.length > 0 ? (
        block.words.map((edit, i) => (
          <p key={i}>
            {edit.before && (
              <span className="rounded bg-danger/10 px-1 line-through">{edit.before}</span>
            )}
            {edit.before && edit.after ? " → " : ""}
            {edit.after && <span className="rounded bg-ok/10 px-1">{edit.after}</span>}
          </p>
        ))
      ) : (
        <p className="text-muted">{block.after}</p>
      )}
    </div>
  );
}

function Retention({ siteId }: { siteId: number }) {
  const client = useQueryClient();
  const [queued, setQueued] = useState<string | null>(null);
  const plan = useQuery({
    queryKey: ["retention", siteId],
    queryFn: () => endpoints.retention(siteId),
  });

  const save = useMutation({
    mutationFn: (body: Record<string, unknown>) => endpoints.putRetention(siteId, body),
    onSuccess: (data) => client.setQueryData(["retention", siteId], data),
  });
  const run = useMutation({
    mutationFn: () => endpoints.applyRetention(siteId),
    onSuccess: (result) => setQueued(`Queued as job #${result.job_id}. This is not reversible.`),
  });

  if (plan.isLoading) return <Spinner className="h-5 w-5 text-muted" />;
  if (!plan.data) return null;

  const data: RetentionPlan = plan.data;
  const policy = data.policy;

  return (
    <div className="space-y-3">
      <h3 className="text-xs font-medium uppercase tracking-wide text-muted">Retention</h3>
      <p className="hint">
        Off by default. A capture is never deleted while it holds the last copy of a page, while
        a later capture deduplicates against it, or while it is this site&rsquo;s first — so the
        rules below decide only what is left after that.
      </p>

      <div className="flex flex-wrap items-end gap-3">
        <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={policy.enabled}
            onChange={(e) => save.mutate({ ...policy, enabled: e.target.checked })}
          />
          Prune automatically
        </label>
        <NumberField
          label="Keep newest"
          value={policy.keep_last}
          onCommit={(v) => save.mutate({ ...policy, keep_last: v })}
        />
        <NumberField
          label="Keep monthly"
          value={policy.keep_monthly}
          onCommit={(v) => save.mutate({ ...policy, keep_monthly: v })}
        />
        <NumberField
          label="Minimum age (days)"
          value={policy.min_age_days}
          onCommit={(v) => save.mutate({ ...policy, min_age_days: v })}
        />
      </div>

      <table className="w-full text-sm">
        <tbody className="divide-y divide-border">
          {data.captures.map((decision) => (
            <tr key={decision.capture_id}>
              <td className="py-1.5 font-mono text-xs">{decision.dir_name}</td>
              <td className="py-1.5 text-xs text-muted">{relative(decision.started_at)}</td>
              <td className="py-1.5 text-right text-xs tabular-nums">
                {bytes(decision.size_bytes)}
              </td>
              <td className="py-1.5 pl-3">
                {decision.keep ? (
                  <span className="text-xs text-muted" title={decision.detail}>
                    kept &mdash; {decision.reason}
                  </span>
                ) : (
                  <span className="text-xs text-warn">would be deleted</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      {data.prunable > 0 ? (
        <div className="flex flex-wrap items-center gap-3">
          <p className="text-sm">
            {data.prunable} capture(s) could go, freeing {bytes(data.freed_bytes)}.
          </p>
          <button
            className="btn-ghost text-danger"
            onClick={() => {
              if (
                confirm(
                  `Delete ${data.prunable} capture(s) of "${data.site_title}"? ` +
                    "This cannot be undone.",
                )
              ) {
                run.mutate();
              }
            }}
            disabled={run.isPending}
          >
            {run.isPending && <Spinner />}
            Prune now
          </button>
        </div>
      ) : (
        <p className="text-sm text-muted">
          Nothing is prunable: every capture is protected by one of the rules above.
        </p>
      )}
      {queued && <p className="text-xs text-muted">{queued}</p>}
      {run.error && <p className="text-xs text-danger">{(run.error as ApiError).message}</p>}
      {data.captures[0] && (
        <p className="hint">Oldest capture: {dateTime(data.captures[0].started_at)}.</p>
      )}
    </div>
  );
}

function NumberField({
  label,
  value,
  onCommit,
}: {
  label: string;
  value: number;
  onCommit: (value: number) => void;
}) {
  const [draft, setDraft] = useState(String(value));
  return (
    <label className="text-xs text-muted">
      <span className="mb-1 block">{label}</span>
      <input
        className="input w-28 text-sm"
        type="number"
        min={0}
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={() => {
          const next = Number(draft);
          if (Number.isFinite(next) && next >= 0 && next !== value) onCommit(next);
        }}
      />
    </label>
  );
}
