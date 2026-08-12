import { useQuery } from "@tanstack/react-query";

import { endpoints, type ApiError, type ReaderBlock } from "../lib/api";
import { readableTimestamp } from "../lib/format";
import { Alert, EmptyState, Spinner } from "./ui";

/**
 * An archived page as text.
 *
 * No iframe, no pywb, no archived JavaScript — this is the app's own origin
 * rendering strings the extractor already pulled out of the WARC, with the
 * sidebar and navigation removed by the same pass that makes search work. It
 * is what you want when you are reading rather than verifying, and it is the
 * one view that still works when a capture missed the stylesheet.
 *
 * It always says which capture it read. A reader view that quietly stood in
 * for a broken replay would make a half-captured site look fine.
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

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-muted">
        <span>
          {data.words.toLocaleString()} words · about {data.minutes} min
          {data.timestamp && ` · captured ${readableTimestamp(data.timestamp)}`}
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

      <article className="card max-w-prose space-y-3 p-6 leading-relaxed">
        {data.title && <h1 className="text-xl font-semibold">{data.title}</h1>}
        <p className="hint break-all font-mono text-[11px]">{data.url}</p>
        {data.blocks.length === 0 ? (
          <Alert kind="warn">
            This page was archived but held no readable text — it may be a redirect, an image,
            or a page built entirely by JavaScript that the crawler could not run.
          </Alert>
        ) : (
          data.blocks.map((block, index) => <Chunk key={index} block={block} />)
        )}
      </article>
    </div>
  );
}

function Chunk({ block }: { block: ReaderBlock }) {
  switch (block.kind) {
    case "h1":
    case "h2":
      return <h2 className="pt-3 text-lg font-medium">{block.text}</h2>;
    case "h3":
      return <h3 className="pt-2 text-base font-medium">{block.text}</h3>;
    case "li":
      return (
        <p className="flex gap-2 pl-2">
          <span aria-hidden className="text-muted">
            ·
          </span>
          <span>{block.text}</span>
        </p>
      );
    case "quote":
      return (
        <blockquote className="border-l-2 border-border pl-4 italic text-muted">
          {block.text}
        </blockquote>
      );
    case "pre":
      return (
        <pre className="overflow-x-auto rounded bg-raised/60 p-3 font-mono text-xs">
          {block.text}
        </pre>
      );
    case "caption":
      return <p className="text-xs text-muted">{block.text}</p>;
    default:
      return <p>{block.text}</p>;
  }
}
