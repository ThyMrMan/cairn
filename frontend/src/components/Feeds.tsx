import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import type { Feed, FeedCandidate } from "../lib/api";
import { ApiError, endpoints } from "../lib/api";
import { relative } from "../lib/format";
import { Alert, PanelHeader, Spinner, useCollapsible } from "./ui";

/**
 * Watching a site for new content.
 *
 * Two things about this panel are deliberate and worth keeping.
 *
 * **The poll history is not a debugging aid, it is the feature.** The
 * ArchiveBox note that a `curl | grep` cron was more dependable than the
 * tool's own scheduler was a judgement about observability rather than
 * correctness — so every poll says what it fetched, what it parsed, what was
 * new and what it did about it, and it is one click away.
 *
 * **Nothing is added without being tested first.** A feed whose entries fall
 * outside the site's scope polls happily forever, finds new posts every time,
 * and archives none of them. That is a miserable thing to diagnose after the
 * fact and a trivial thing to catch at the moment somebody pastes the URL.
 */
export function Feeds({ siteId }: { siteId: number }) {
  const [adding, setAdding] = useState(false);
  const { open, toggle } = useCollapsible("feeds", true);
  const feeds = useQuery({ queryKey: ["feeds", siteId], queryFn: () => endpoints.feeds(siteId) });

  const rows = feeds.data ?? [];
  const pending = rows.reduce((total, feed) => total + feed.counts.pending, 0);

  return (
    <section className="card p-5">
      <PanelHeader
        title="Feeds and watchers"
        hint={
          // The count belongs in the header, because it is the thing you would
          // have opened the panel to find out.
          rows.length > 0
            ? `${rows.length} watched${pending > 0 ? `, ${pending} pending` : ""}.`
            : "New posts are archived into this site's own folder, without re-crawling it."
        }
        open={open}
        onToggle={toggle}
        extra={
          <button className="btn-ghost text-xs" onClick={() => setAdding((v) => !v)}>
            {adding ? "Cancel" : "+ Add a feed"}
          </button>
        }
      />

      {open && adding && (
        <div className="mt-4">
          <AddFeed siteId={siteId} onDone={() => setAdding(false)} />
        </div>
      )}

      {open && feeds.isLoading && <Spinner className="mt-4 h-4 w-4 text-muted" />}

      {open && !feeds.isLoading && rows.length === 0 && !adding && (
        <p className="mt-4 text-sm text-muted">
          Nothing is being watched. Add a feed, or press <em>Index</em> on this site — discovery
          attaches whatever it finds.
        </p>
      )}

      {open && rows.length > 0 && (
        <ul className="mt-4 space-y-2">
          {rows.map((feed) => (
            <FeedRow key={feed.id} feed={feed} siteId={siteId} />
          ))}
        </ul>
      )}

      {open && pending > 0 && (
        <p className="hint mt-3">
          {pending} item(s) are waiting to be captured. They go on the next scheduled pass, or
          use <em>Capture pending</em> on the feed to do it now.
        </p>
      )}
    </section>
  );
}

function FeedRow({ feed, siteId }: { feed: Feed; siteId: number }) {
  const client = useQueryClient();
  const [open, setOpen] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const refresh = () => client.invalidateQueries({ queryKey: ["feeds", siteId] });

  const poll = useMutation({
    mutationFn: () => endpoints.pollFeed(feed.id),
    onSuccess: (result) => {
      setNote(
        result.baseline
          ? `Recorded ${result.entries_seen} existing item(s) as the starting point. ` +
            "Nothing was captured — they are what the site already had."
          : `${result.action}. ${result.job_ids.length} capture job(s) queued.`,
      );
      void refresh();
    },
    onError: (err) => setNote((err as ApiError).message),
  });

  const capture = useMutation({
    mutationFn: () => endpoints.captureFeed(feed.id),
    onSuccess: (result) => {
      setNote(result.action);
      void refresh();
    },
  });

  const update = useMutation({
    mutationFn: (body: Record<string, unknown>) => endpoints.updateFeed(feed.id, body),
    onSuccess: () => refresh(),
  });

  const remove = useMutation({
    mutationFn: () => endpoints.deleteFeed(feed.id),
    onSuccess: () => refresh(),
  });

  const failing = feed.consecutive_failures > 0;

  return (
    <li className="rounded-md border border-border">
      <div className="flex flex-wrap items-start gap-3 p-3">
        <span
          className={`mt-1 h-2 w-2 shrink-0 rounded-full ${
            !feed.enabled ? "bg-muted" : failing ? "bg-warn" : "bg-ok"
          }`}
          aria-hidden="true"
        />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-sm font-medium">{feed.title || "Untitled feed"}</span>
            <span className="rounded bg-raised px-1.5 py-0.5 text-[10px] uppercase text-muted">
              {feed.kind}
            </span>
            <span className="text-xs text-muted">every {humanInterval(feed.interval_min)}</span>
            {!feed.auto_capture && (
              <span className="text-xs text-muted">· capture off</span>
            )}
          </div>
          <p className="truncate font-mono text-[11px] text-muted">{feed.url}</p>
          <p className="mt-1 text-xs text-muted">
            {feed.last_polled_at ? `Polled ${relative(feed.last_polled_at)}` : "Never polled"}
            {feed.last_status ? ` · ${statusText(feed.last_status)}` : ""}
            {feed.enabled && feed.next_poll_at ? ` · next ${relative(feed.next_poll_at)}` : ""}
          </p>
          <p className="mt-0.5 text-xs text-muted tabular-nums">
            {feed.counts.seen} seen · {feed.counts.captured} captured · {feed.counts.pending}{" "}
            pending
            {feed.counts.gone > 0 && ` · ${feed.counts.gone} gone from the site`}
          </p>
        </div>
        <div className="flex shrink-0 flex-wrap gap-1.5">
          <button className="btn-ghost px-2 text-xs" disabled={poll.isPending}
                  onClick={() => poll.mutate()}>
            {poll.isPending && <Spinner className="mr-1 h-3 w-3" />}
            Poll now
          </button>
          {feed.counts.pending > 0 && (
            <button className="btn-ghost px-2 text-xs" onClick={() => capture.mutate()}>
              Capture pending
            </button>
          )}
          <button className="btn-ghost px-2 text-xs" onClick={() => setOpen((v) => !v)}>
            {open ? "Hide" : "History"}
          </button>
        </div>
      </div>

      {feed.disabled_reason && (
        <div className="border-t border-border p-3">
          <Alert kind="warn">
            {feed.disabled_reason}
            <button
              className="btn-ghost ml-2 px-2 text-xs"
              onClick={() => update.mutate({ enabled: true })}
            >
              Turn it back on
            </button>
          </Alert>
        </div>
      )}

      {feed.last_error && !feed.disabled_reason && (
        <p className="border-t border-border px-3 py-2 text-xs text-warn">
          Last poll failed: {feed.last_error}
          {feed.consecutive_failures > 1 &&
            ` (${feed.consecutive_failures} in a row — it is being retried less often)`}
        </p>
      )}

      {note && <p className="border-t border-border px-3 py-2 text-xs text-muted">{note}</p>}

      {open && (
        <div className="space-y-4 border-t border-border p-3">
          <PollHistory feedId={feed.id} />
          <div className="flex flex-wrap items-center gap-4 text-xs">
            <label className="flex items-center gap-1.5">
              <input
                type="checkbox"
                checked={feed.enabled}
                onChange={(e) => update.mutate({ enabled: e.target.checked })}
              />
              Poll it
            </label>
            <label className="flex items-center gap-1.5">
              <input
                type="checkbox"
                checked={feed.auto_capture}
                onChange={(e) => update.mutate({ auto_capture: e.target.checked })}
              />
              Capture new items automatically
            </label>
            <label className="flex items-center gap-1.5" title="Most feeds touch the updated
              timestamp for trivial reasons, so this turns into constant churn.">
              <input
                type="checkbox"
                checked={feed.recapture_on_update}
                onChange={(e) => update.mutate({ recapture_on_update: e.target.checked })}
              />
              Re-capture edited posts
            </label>
            <label className="flex items-center gap-1.5">
              Every
              <select
                className="field w-auto py-0.5 text-xs"
                value={feed.interval_min}
                onChange={(e) => update.mutate({ interval_min: Number(e.target.value) })}
              >
                {[15, 60, 180, 360, 720, 1440, 10080].map((mins) => (
                  <option key={mins} value={mins}>
                    {humanInterval(mins)}
                  </option>
                ))}
              </select>
            </label>
            <button
              className="btn-ghost px-2 text-xs text-danger"
              onClick={() => {
                if (confirm("Stop watching this feed? Captures already made are kept.")) {
                  remove.mutate();
                }
              }}
            >
              Remove
            </button>
          </div>
        </div>
      )}
    </li>
  );
}

function PollHistory({ feedId }: { feedId: number }) {
  const polls = useQuery({
    queryKey: ["feed-polls", feedId],
    queryFn: () => endpoints.feedPolls(feedId),
  });

  if (polls.isLoading) return <Spinner className="h-4 w-4 text-muted" />;
  if (!polls.data?.length) {
    return <p className="text-xs text-muted">This feed has not been polled yet.</p>;
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-[11px]">
        <thead className="text-muted">
          <tr className="text-left">
            <th className="py-1 pr-3 font-medium">When</th>
            <th className="py-1 pr-3 font-medium">Status</th>
            <th className="py-1 pr-3 text-right font-medium">Entries</th>
            <th className="py-1 pr-3 text-right font-medium">New</th>
            <th className="py-1 pr-3 font-medium">What it did</th>
          </tr>
        </thead>
        <tbody>
          {polls.data.map((poll) => (
            <tr key={poll.id} className="border-t border-border/60">
              <td className="py-1 pr-3 text-muted">{relative(poll.ts)}</td>
              <td className={`py-1 pr-3 tabular-nums ${poll.error ? "text-warn" : ""}`}>
                {statusText(poll.status)}
              </td>
              <td className="py-1 pr-3 text-right tabular-nums">{poll.entries_seen || "—"}</td>
              <td className="py-1 pr-3 text-right tabular-nums">{poll.new_items || "—"}</td>
              <td className="py-1 pr-3 text-muted">{poll.error || poll.action}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** Paste a URL, or let the site tell you what it has. Nothing saves untested. */
function AddFeed({ siteId, onDone }: { siteId: number; onDone: () => void }) {
  const client = useQueryClient();
  const [url, setUrl] = useState("");
  const [asPage, setAsPage] = useState(false);
  const [tested, setTested] = useState<FeedCandidate | null>(null);

  const test = useMutation({
    mutationFn: () => endpoints.testFeed(siteId, url, asPage ? "page" : "auto"),
    onSuccess: setTested,
  });

  const find = useMutation({ mutationFn: () => endpoints.discoverFeeds(siteId) });

  const add = useMutation({
    mutationFn: (candidate: FeedCandidate) =>
      endpoints.addFeed(siteId, {
        url: candidate.url,
        kind: candidate.kind === "sitemap" ? "sitemap" : asPage ? "page" : "auto",
        title: candidate.title,
        // A comment feed is mostly noise, and a feed whose entries the scope
        // would refuse cannot capture anything — so both arrive watched but
        // not capturing, rather than quietly doing nothing.
        auto_capture: !candidate.is_comments && candidate.in_scope > 0,
      }),
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey: ["feeds", siteId] });
      onDone();
    },
  });

  return (
    <div className="space-y-3 rounded-md border border-border bg-raised/40 p-3">
      <div className="flex flex-wrap gap-2">
        <input
          className="field min-w-0 flex-1 py-1 font-mono text-xs"
          placeholder="https://example.com/feed"
          value={url}
          onChange={(e) => {
            setUrl(e.target.value);
            setTested(null);
          }}
          aria-label="Feed URL"
        />
        <button
          className="btn-ghost text-xs"
          disabled={!url.trim() || test.isPending}
          onClick={() => test.mutate()}
        >
          {test.isPending && <Spinner className="mr-1 h-3 w-3" />}
          Test it
        </button>
        <button className="btn-ghost text-xs" disabled={find.isPending}
                onClick={() => find.mutate()}>
          {find.isPending && <Spinner className="mr-1 h-3 w-3" />}
          Find feeds
        </button>
      </div>

      <label className="flex items-center gap-2 text-xs text-muted">
        <input
          type="checkbox"
          checked={asPage}
          onChange={(e) => {
            setAsPage(e.target.checked);
            setTested(null);
          }}
        />
        Watch this URL as a page, not a feed
      </label>
      {asPage && (
        <p className="hint">
          For a site with no feed. The page is fetched on a schedule and captured when its
          readable text changes — not when its markup does, so a visit counter or a rotating
          advert in the furniture will not set it off.
        </p>
      )}

      {test.error && <Alert kind="error">{(test.error as ApiError).message}</Alert>}
      {add.error && <Alert kind="error">{(add.error as ApiError).message}</Alert>}

      {tested && <Candidate candidate={tested} onAdd={() => add.mutate(tested)} />}

      {find.data && (
        <div className="space-y-2">
          <p className="hint">
            {find.data.length
              ? "Found on this site. Nothing is watched until you add it."
              : "Nothing new found — anything this site publishes is already attached."}
          </p>
          {find.data.map((candidate) => (
            <Candidate
              key={candidate.url}
              candidate={candidate}
              onAdd={() => add.mutate(candidate)}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function Candidate({ candidate, onAdd }: { candidate: FeedCandidate; onAdd: () => void }) {
  const outOfScope = candidate.entry_count > 0 && candidate.in_scope === 0;

  return (
    <div className="rounded border border-border bg-bg p-2.5">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-sm">{candidate.title || candidate.url}</span>
            <span className="rounded bg-raised px-1.5 py-0.5 text-[10px] uppercase text-muted">
              {candidate.kind}
            </span>
            {candidate.is_comments && (
              <span className="text-[10px] text-muted">comments — usually noise</span>
            )}
          </div>
          <p className="truncate font-mono text-[11px] text-muted">{candidate.url}</p>
        </div>
        <button className="btn-ghost px-2 text-xs" onClick={onAdd} disabled={!candidate.ok}>
          Watch it
        </button>
      </div>

      {candidate.error ? (
        <p className="mt-1.5 text-xs text-danger">{candidate.error}</p>
      ) : (
        <>
          <p className="mt-1.5 text-xs text-muted">
            {candidate.kind === "sitemap"
              ? "A sitemap watcher: it notices anything the feed cannot carry, including pages that disappear."
              : `${candidate.entry_count} entries right now`}
          </p>
          {candidate.recent_titles.length > 0 && (
            <ul className="mt-1 space-y-0.5 text-[11px] text-muted">
              {candidate.recent_titles.map((title) => (
                <li key={title} className="truncate">
                  · {title}
                </li>
              ))}
            </ul>
          )}
          {outOfScope && (
            <div className="mt-2">
              <Alert kind="warn">
                Its entries are outside this site's crawl scope, so capturing them would fetch
                nothing. Add the host in <em>Domains and crawl scope</em> first, or watch it
                without automatic capture.
                <span className="mt-1 block font-mono text-[11px] opacity-80">
                  {candidate.out_of_scope[0]}
                </span>
              </Alert>
            </div>
          )}
        </>
      )}
    </div>
  );
}

function humanInterval(minutes: number): string {
  if (minutes < 60) return `${minutes}m`;
  if (minutes < 1440) return `${Math.round(minutes / 60)}h`;
  const days = Math.round(minutes / 1440);
  return days === 7 ? "week" : `${days}d`;
}

function statusText(status: number): string {
  if (!status) return "no response";
  if (status === 304) return "304 unchanged";
  return String(status);
}
