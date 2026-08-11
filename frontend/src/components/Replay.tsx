import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState, type FormEvent } from "react";

import { endpoints, type CdxVersion } from "../lib/api";
import { dateTime, readableTimestamp } from "../lib/format";
import { Alert, EmptyState, Spinner } from "./ui";

/**
 * Browsing the archive.
 *
 * pywb renders the page inside the iframe; everything around it is ours. That
 * split is deliberate and is the whole reason the chrome is not part of the
 * replayed document: archived CSS cannot restyle controls it never receives,
 * and archived JavaScript cannot fake a capture selector it cannot reach.
 *
 * The iframe is on a different origin for the same reason — archived pages
 * run their own JavaScript in your browser, and on a shared origin that code
 * could read the session cookie and call the API as you (docs/07, docs/11).
 */
export function Replay({
  siteId,
  initialUrl = "",
  initialTimestamp = null,
}: {
  siteId: number;
  /** Open here instead of at the seed — a search result arriving at its page. */
  initialUrl?: string;
  initialTimestamp?: string | null;
}) {
  const client = useQueryClient();
  const status = useQuery({
    queryKey: ["replay", siteId],
    queryFn: () => endpoints.replayStatus(siteId),
  });

  const [url, setUrl] = useState(initialUrl);
  const [draft, setDraft] = useState(initialUrl);
  const [timestamp, setTimestamp] = useState<string | null>(initialTimestamp);

  // The seed is the way in; everything else is reached by clicking.
  useEffect(() => {
    if (status.data && !url) {
      setUrl(status.data.seed_url);
      setDraft(status.data.seed_url);
    }
  }, [status.data, url]);

  const versions = useQuery({
    queryKey: ["replay-versions", siteId, url],
    queryFn: () => endpoints.replayVersions(siteId, url),
    enabled: Boolean(url),
  });

  const reindex = useMutation({
    mutationFn: () => endpoints.reindex(siteId),
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey: ["replay", siteId] });
      await client.invalidateQueries({ queryKey: ["replay-versions", siteId] });
    },
  });

  if (status.isLoading) return <Spinner className="h-5 w-5 text-muted" />;
  if (!status.data) return <Alert kind="error">Replay status could not be loaded.</Alert>;

  const data = status.data;
  const list = versions.data?.versions ?? [];
  const current = timestamp ?? list.at(-1)?.timestamp ?? null;

  if (data.records === 0) {
    return (
      <EmptyState title="Nothing indexed yet">
        <p>
          Replay reads an index built from this site&rsquo;s WARCs. Capture the site, or
          rebuild the index if the captures are already there.
        </p>
        <button className="btn-ghost mt-3" onClick={() => reindex.mutate()} disabled={reindex.isPending}>
          {reindex.isPending && <Spinner />}
          Rebuild index
        </button>
        {reindex.data && (
          <p className="hint mt-2">
            {reindex.data.records} record(s) from {reindex.data.warcs} WARC(s).
          </p>
        )}
      </EmptyState>
    );
  }

  if (!data.base_url) {
    return (
      <Alert kind="error" title="Replay has no address">
        The replay origin could not be worked out from this request. Set
        <code className="mx-1 font-mono">CAIRN_REPLAY_PUBLIC_URL</code>
        and reload.
      </Alert>
    );
  }

  const src = current ? `${data.base_url}/${current}/${url}` : `${data.base_url}/${url}`;

  function go(e: FormEvent) {
    e.preventDefault();
    setUrl(draft.trim());
    setTimestamp(null);
  }

  return (
    <div className="space-y-3">
      {data.shares_host_with_app && (
        <Alert kind="warn" title="Replay is not isolated from this app">
          Ports do not separate cookies, so a page in the archive could read your session.
          Give replay its own hostname before exposing this instance to the internet.
        </Alert>
      )}

      <div className="flex flex-wrap items-center gap-2">
        <form onSubmit={go} className="flex min-w-0 flex-1 gap-2">
          <input
            className="field min-w-0 flex-1 font-mono text-xs"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            spellCheck={false}
            aria-label="Archived URL"
          />
          <button className="btn-ghost shrink-0">Go</button>
        </form>

        <select
          className="field w-auto shrink-0 text-xs"
          value={current ?? ""}
          onChange={(e) => setTimestamp(e.target.value)}
          aria-label="Capture"
          disabled={list.length === 0}
        >
          {list.map((v) => (
            <option key={v.timestamp} value={v.timestamp}>
              {readableTimestamp(v.timestamp)}
            </option>
          ))}
        </select>

        <span className="shrink-0 text-xs text-muted">
          {list.length === 1 ? "1 version" : `${list.length} versions`}
        </span>

        <a
          className="btn-ghost shrink-0 text-xs"
          href={src}
          target="_blank"
          rel="noreferrer noopener"
        >
          Open ↗
        </a>
      </div>

      {versions.data?.count === 0 && (
        <Alert kind="warn">
          That URL is not in this archive. It may have been out of scope, or never linked
          from a page that was captured.
        </Alert>
      )}

      <iframe
        // Keyed so switching capture or URL remounts rather than leaving the
        // previous page on screen while the next one loads.
        key={src}
        src={src}
        title="Archived page"
        className="h-[70vh] w-full rounded-md border border-border bg-white"
        // Archived JavaScript needs to run, and needs same-origin *relative to
        // the replay origin* to work at all. The sandbox still denies
        // top-level navigation, so a page in the archive cannot replace this
        // one, and popups open sandboxed.
        sandbox="allow-scripts allow-same-origin allow-forms allow-popups"
        referrerPolicy="no-referrer"
      />

      <div className="flex items-center justify-between">
        <p className="hint">
          {data.records.toLocaleString()} records indexed
          {data.indexed_at ? ` · ${dateTime(new Date(data.indexed_at * 1000).toISOString())}` : ""}
        </p>
        <button className="btn-ghost text-xs" onClick={() => reindex.mutate()} disabled={reindex.isPending}>
          {reindex.isPending && <Spinner />}
          Rebuild index
        </button>
      </div>
    </div>
  );
}

export type { CdxVersion };
