import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import {
  endpoints,
  type Annotation,
  type ApiError,
  type ReaderArticle,
  type ReaderBlock,
} from "../lib/api";
import { readableTimestamp } from "../lib/format";
import { Alert, EmptyState, Spinner } from "./ui";

/**
 * An archived page as text, with notes on it.
 *
 * No iframe, no pywb, no archived JavaScript — this is the app's own origin
 * rendering strings the extractor already pulled out of the WARC, with the
 * sidebar and navigation removed by the same pass that makes search work. It
 * is what you want when you are reading rather than verifying, and it is the
 * one view that still works when a capture missed the stylesheet.
 *
 * It always says which capture it read. A reader view that quietly stood in
 * for a broken replay would make a half-captured site look fine.
 *
 * Annotations live here rather than on replay because they *can* only live
 * here: replay is a separate origin so archived JavaScript cannot reach this
 * application, which means this application cannot read a selection out of it.
 */
export function Reader({
  siteId,
  url,
  captureDir,
  onCapture,
}: {
  siteId: number;
  url: string;
  captureDir: string | null;
  onCapture: (dir: string | null) => void;
}) {
  const client = useQueryClient();
  const [pending, setPending] = useState<PendingNote | null>(null);

  const article = useQuery({
    queryKey: ["reader", siteId, url, captureDir],
    queryFn: () => endpoints.readerPage(siteId, url, captureDir ?? undefined),
    enabled: Boolean(url),
    retry: false,
  });
  const versions = useQuery({
    queryKey: ["reader-versions", siteId, url],
    queryFn: () => endpoints.readerVersions(siteId, url),
    enabled: Boolean(url),
  });

  const refresh = () => client.invalidateQueries({ queryKey: ["reader", siteId, url] });
  const add = useMutation({
    mutationFn: (body: Parameters<typeof endpoints.addAnnotation>[1]) =>
      endpoints.addAnnotation(siteId, body),
    onSuccess: async () => {
      setPending(null);
      await refresh();
    },
  });
  const drop = useMutation({
    mutationFn: (id: number) => endpoints.deleteAnnotation(id),
    onSuccess: refresh,
  });

  if (article.isLoading) return <Spinner className="h-5 w-5 text-muted" />;

  if (article.error) {
    const err = article.error as ApiError;
    return (
      <EmptyState title="Nothing to read here">
        <p>{err.message}</p>
      </EmptyState>
    );
  }

  const data = article.data;
  if (!data) return null;
  const list = versions.data?.versions ?? [];
  const lost = data.annotations.filter((a) => !a.found);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-muted">
        <span>
          {data.words.toLocaleString()} words · about {data.minutes} min
          {data.timestamp && ` · captured ${readableTimestamp(data.timestamp)}`}
          {data.annotations.length > 0 && ` · ${data.annotations.length} note(s)`}
        </span>
        {list.length > 1 && (
          <label className="flex items-center gap-2">
            Version
            <select
              className="field w-auto py-1 text-xs"
              value={captureDir ?? data.capture_dir}
              onChange={(e) => onCapture(e.target.value)}
            >
              {list.map((version) => (
                <option key={version.capture_dir} value={version.capture_dir}>
                  {version.timestamp
                    ? readableTimestamp(version.timestamp)
                    : version.capture_dir}
                </option>
              ))}
            </select>
          </label>
        )}
      </div>

      <article
        className="card max-w-prose space-y-3 p-6 leading-relaxed"
        onMouseUp={() => setPending(readSelection(data))}
      >
        {data.title && <h1 className="text-xl font-semibold">{data.title}</h1>}
        <p className="hint break-all font-mono text-[11px]">{data.url}</p>
        {data.blocks.length === 0 ? (
          <Alert kind="warn">
            This page was archived but held no readable text — it may be a redirect, an image,
            or a page built entirely by JavaScript that the crawler could not run.
          </Alert>
        ) : (
          data.blocks.map((block, index) => (
            <Chunk
              key={index}
              block={block}
              marks={data.annotations.filter((a) => a.found && a.block_index === index)}
            />
          ))
        )}
      </article>

      {pending && (
        <NoteEditor
          pending={pending}
          busy={add.isPending}
          error={add.error ? (add.error as ApiError).message : null}
          onCancel={() => setPending(null)}
          onSave={(note) => add.mutate({ ...pending.body, url: data.url, note })}
        />
      )}

      {lost.length > 0 && (
        <Alert kind="warn" title={`${lost.length} note(s) do not fit this version`}>
          <p>
            The text they quote is not in this capture — the page was edited, or you are looking
            at a different version of it. They are kept, not moved: an annotation that quietly
            attaches itself to the nearest sentence is worse than one that says it is lost.
          </p>
          <ul className="mt-2 space-y-2">
            {lost.map((mark) => (
              <li key={mark.id} className="text-sm">
                <q className="text-muted">{mark.quote}</q>
                {mark.note && <p className="mt-0.5">{mark.note}</p>}
              </li>
            ))}
          </ul>
        </Alert>
      )}

      {data.annotations.filter((a) => a.found && a.note).length > 0 && (
        <section className="space-y-2">
          <h3 className="text-xs font-medium uppercase text-muted">Notes</h3>
          <ul className="space-y-2">
            {data.annotations
              .filter((a) => a.found && a.note)
              .map((mark) => (
                <li key={mark.id} className="card p-3 text-sm">
                  <q className="text-muted">{mark.quote}</q>
                  <p className="mt-1">{mark.note}</p>
                  <button
                    className="btn-ghost mt-2 px-2 py-0.5 text-xs"
                    onClick={() => drop.mutate(mark.id)}
                  >
                    Delete
                  </button>
                </li>
              ))}
          </ul>
        </section>
      )}
    </div>
  );
}

type PendingNote = {
  quote: string;
  body: {
    url: string;
    quote: string;
    prefix: string;
    suffix: string;
    block_index: number;
  };
};

/**
 * What the reader selected, and where.
 *
 * The block index and the surrounding characters come from the rendered text
 * rather than from DOM ranges: the server anchors by quotation, so what it
 * needs is the string and enough of its neighbours to tell two identical
 * sentences apart.
 */
function readSelection(article: ReaderArticle): PendingNote | null {
  const selection = window.getSelection();
  const quote = (selection?.toString() ?? "").replace(/\s+/g, " ").trim();
  if (!quote || quote.length < 2) return null;

  for (let index = 0; index < article.blocks.length; index++) {
    const text = article.blocks[index]?.text ?? "";
    const at = text.indexOf(quote);
    if (at < 0) continue;
    return {
      quote,
      body: {
        url: article.url,
        quote,
        prefix: text.slice(Math.max(0, at - 40), at),
        suffix: text.slice(at + quote.length, at + quote.length + 40),
        block_index: index,
      },
    };
  }
  // A selection spanning two blocks has no single quotation to anchor to.
  return null;
}

function NoteEditor({
  pending,
  busy,
  error,
  onSave,
  onCancel,
}: {
  pending: PendingNote;
  busy: boolean;
  error: string | null;
  onSave: (note: string) => void;
  onCancel: () => void;
}) {
  const [note, setNote] = useState("");
  return (
    <div className="card space-y-2 p-4">
      <p className="text-sm">
        <q className="text-muted">{pending.quote}</q>
      </p>
      <textarea
        className="field h-20 w-full text-sm"
        placeholder="A note, or leave it empty to just highlight"
        value={note}
        onChange={(e) => setNote(e.target.value)}
        aria-label="Note"
        autoFocus
      />
      <div className="flex gap-2">
        <button className="btn-primary" onClick={() => onSave(note)} disabled={busy}>
          {busy && <Spinner />}
          Save
        </button>
        <button className="btn-ghost" onClick={onCancel}>
          Cancel
        </button>
      </div>
      {error && <p className="text-xs text-danger">{error}</p>}
    </div>
  );
}

/** One block, with any highlights spliced into it. Text in, elements out. */
function Chunk({ block, marks }: { block: ReaderBlock; marks: Annotation[] }) {
  const body = marks.length ? highlighted(block.text, marks) : block.text;

  switch (block.kind) {
    case "h1":
    case "h2":
      return <h2 className="pt-3 text-lg font-medium">{body}</h2>;
    case "h3":
      return <h3 className="pt-2 text-base font-medium">{body}</h3>;
    case "li":
      return (
        <p className="flex gap-2 pl-2">
          <span aria-hidden className="text-muted">
            ·
          </span>
          <span>{body}</span>
        </p>
      );
    case "quote":
      return (
        <blockquote className="border-l-2 border-border pl-4 italic text-muted">{body}</blockquote>
      );
    case "pre":
      return (
        <pre className="overflow-x-auto rounded bg-raised/60 p-3 font-mono text-xs">{body}</pre>
      );
    case "caption":
      return <p className="text-xs text-muted">{body}</p>;
    default:
      return <p>{body}</p>;
  }
}

function highlighted(text: string, marks: Annotation[]) {
  // Sorted and de-overlapped: two notes on the same sentence would otherwise
  // splice the string twice and duplicate the text between them.
  const spans = [...marks].sort((a, b) => a.start - b.start);
  const parts: (string | JSX.Element)[] = [];
  let cursor = 0;
  for (const mark of spans) {
    if (mark.start < cursor) continue;
    if (mark.start > cursor) parts.push(text.slice(cursor, mark.start));
    parts.push(
      <mark
        key={mark.id}
        className="rounded bg-warn/25 px-0.5"
        title={mark.note ?? "Highlighted"}
      >
        {text.slice(mark.start, mark.end)}
      </mark>,
    );
    cursor = mark.end;
  }
  if (cursor < text.length) parts.push(text.slice(cursor));
  return parts;
}
