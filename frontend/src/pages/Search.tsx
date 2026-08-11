import { useMutation, useQuery } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { Alert, EmptyState, Spinner } from "../components/ui";
import { endpoints, type SearchHit } from "../lib/api";
import { readableTimestamp } from "../lib/format";

/**
 * Search across every archive.
 *
 * The query lives in the URL, so a search is a link you can send yourself and
 * the back button does what it looks like it does.
 *
 * Snippets arrive as plain text with the matched terms known separately, and
 * the highlighting is done here by splitting on those terms — never by
 * putting server-built HTML into the page. The text being highlighted comes
 * out of an archived website, which is the last string in this application
 * that should be trusted with markup.
 */
export default function Search() {
  const [params, setParams] = useSearchParams();
  const q = params.get("q") ?? "";
  const [draft, setDraft] = useState(q);

  const results = useQuery({
    queryKey: ["search", q],
    queryFn: () => endpoints.search({ q, limit: 50 }),
    enabled: q.trim().length > 0,
  });
  const status = useQuery({ queryKey: ["search-status"], queryFn: endpoints.searchStatus });

  const reindex = useMutation({
    mutationFn: () => endpoints.reindexSearch({ extract: true }),
  });

  function submit(event: FormEvent) {
    event.preventDefault();
    setParams(draft.trim() ? { q: draft.trim() } : {});
  }

  const indexed = status.data;
  const empty = indexed !== undefined && indexed.pages === 0;

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-xl font-semibold tracking-tight">Search</h1>
        <p className="hint mt-1">
          {indexed
            ? `${indexed.pages.toLocaleString()} page(s) from ${indexed.sites.toLocaleString()} site(s), ${indexed.words.toLocaleString()} words.`
            : " "}
        </p>
      </div>

      <form onSubmit={submit} className="flex gap-2">
        <input
          className="input flex-1"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder="Words to look for, or &quot;an exact phrase&quot;"
          autoFocus
          aria-label="Search the archive"
        />
        <button className="btn-primary" type="submit">
          Search
        </button>
      </form>

      <p className="hint">
        <code>&quot;an exact phrase&quot;</code> matches those words in order, a trailing{" "}
        <code>*</code> matches a prefix, and a leading <code>-</code> excludes. Everything else
        is a word, including <code>AND</code> and <code>OR</code>.
      </p>

      {empty && (
        <EmptyState title="Nothing indexed yet">
          <p>
            Text is extracted after each capture. Captures made before this existed are not in
            the index until they are read again.
          </p>
          <button
            className="btn-ghost mt-3"
            onClick={() => reindex.mutate()}
            disabled={reindex.isPending}
          >
            {reindex.isPending && <Spinner />}
            Index existing captures
          </button>
          {reindex.data && (
            <p className="hint mt-2">
              Queued as job #{reindex.data.job_id}. It reads every WARC, so give it a while.
            </p>
          )}
        </EmptyState>
      )}

      {!empty && indexed && indexed.unindexed_sites.length > 0 && (
        <Alert kind="warn" title={`${indexed.unindexed_sites.length} site(s) are not searchable`}>
          <p>
            They have captures but no extracted text — most likely captured before extraction
            existed.
          </p>
          <button
            className="btn-ghost mt-2"
            onClick={() => reindex.mutate()}
            disabled={reindex.isPending}
          >
            {reindex.isPending && <Spinner />}
            Index them now
          </button>
        </Alert>
      )}

      {results.isFetching && <Spinner className="h-5 w-5 text-muted" />}

      {results.data && (
        <div className="space-y-3">
          <p className="text-sm text-muted">
            {results.data.total.toLocaleString()}
            {results.data.truncated ? "+" : ""} result(s)
          </p>
          {results.data.hits.map((hit) => (
            <Result key={`${hit.site_id}:${hit.url}`} hit={hit} terms={results.data.terms} />
          ))}
          {results.data.total === 0 && (
            <EmptyState title="No matches">
              Nothing in the archive contains that. Try fewer words, or a prefix like{" "}
              <code>machair*</code>.
            </EmptyState>
          )}
        </div>
      )}
    </div>
  );
}

function Result({ hit, terms }: { hit: SearchHit; terms: string[] }) {
  const replay = `/sites/${hit.site_id}?replay=${encodeURIComponent(hit.url)}${
    hit.timestamp ? `&ts=${hit.timestamp}` : ""
  }`;

  return (
    <article className="card space-y-2 p-4">
      <div className="flex items-baseline justify-between gap-3">
        <Link to={replay} className="text-sm font-medium text-accent hover:underline">
          {hit.title || hit.url}
        </Link>
        <span className="shrink-0 text-xs text-muted tabular-nums">
          {hit.timestamp ? readableTimestamp(hit.timestamp) : ""}
        </span>
      </div>
      <p className="break-all text-xs text-muted">{hit.url}</p>
      {hit.snippets.map((snippet, i) => (
        <p key={i} className="text-sm leading-relaxed">
          <Highlighted text={snippet} terms={terms} />
        </p>
      ))}
      <p className="hint">
        <Link to={`/sites/${hit.site_id}`} className="hover:underline">
          {hit.folder_path ? `${hit.folder_path} / ` : ""}
          {hit.site_title}
        </Link>
        {" · "}
        {hit.words.toLocaleString()} words
      </p>
    </article>
  );
}

/** Split on the matched terms and wrap them. Text in, elements out — no HTML. */
function Highlighted({ text, terms }: { text: string; terms: string[] }) {
  const needles = terms.filter(Boolean).map(escapeRegExp);
  if (!needles.length) return <>{text}</>;

  // A capturing group puts the matches at the odd indices of the split, which
  // is the whole test. Re-running `pattern.test` per part would not work: a
  // global regex carries `lastIndex` between calls and alternates.
  const parts = text.split(new RegExp(`(${needles.join("|")})`, "gi"));
  return (
    <>
      {parts.map((part, i) =>
        i % 2 === 1 ? (
          <mark key={i} className="rounded bg-accent/20 px-0.5 text-fg">
            {part}
          </mark>
        ) : (
          <span key={i}>{part}</span>
        ),
      )}
    </>
  );
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}
