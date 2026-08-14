import { useEffect, useRef, useState } from "react";

import { Spinner } from "./ui";

type Line = { id: number; level: string; text: string; kind: string };

const MAX_LINES = 2000;

/**
 * Live capture output over SSE.
 *
 * EventSource rather than fetch streaming: it reconnects on its own and sends
 * `Last-Event-ID`, which the server uses to replay the events that were missed
 * from its ring buffer. A log viewer that silently drops the lines it missed
 * is worse than one that admits it did — so the server's `lagged` event is
 * rendered rather than swallowed.
 */
export function LiveLog({
  jobId,
  onFinished,
}: {
  jobId: number;
  onFinished?: (status: string) => void;
}) {
  const [lines, setLines] = useState<Line[]>([]);
  // `unit` because the engines do not count the same thing: browsertrix has
  // no per-URL record on stdout and reports pages, wget reports URLs.
  const [progress, setProgress] = useState<{
    done: number;
    bytes: number;
    unit?: string;
  } | null>(null);
  const [status, setStatus] = useState<string>("running");
  const boxRef = useRef<HTMLDivElement>(null);
  const pinnedRef = useRef(true);
  const counter = useRef(0);
  const finishedRef = useRef(onFinished);
  finishedRef.current = onFinished;

  useEffect(() => {
    setLines([]);
    setProgress(null);
    setStatus("running");

    const source = new EventSource(`/api/jobs/${jobId}/events`);
    const push = (kind: string, level: string, text: string) =>
      setLines((prev) => {
        const next = [...prev, { id: counter.current++, kind, level, text }];
        // Bounded: a long crawl emits far more than a browser should hold.
        return next.length > MAX_LINES ? next.slice(-MAX_LINES) : next;
      });

    const on = (name: string, handler: (data: Record<string, unknown>) => void) =>
      source.addEventListener(name, (event) => {
        try {
          handler(JSON.parse((event as MessageEvent<string>).data));
        } catch {
          /* a malformed frame must not break the viewer */
        }
      });

    on("log", (d) => {
      const level = String(d.level ?? "info");
      // The engine logs its full command line at debug level. It is genuinely
      // useful, but as the first thing in every capture it buries the crawl
      // under a wall of flags. The scope panel shows the same arguments on
      // demand, so the live view stays about what is happening.
      if (level !== "debug") push("log", level, String(d.msg ?? ""));
    });
    on("url", (d) =>
      push(
        "url",
        d.error || Number(d.status) >= 400 ? "error" : "info",
        `${d.status ?? "ERR"}  ${d.url}${d.error ? `  — ${d.error}` : ""}`,
      ),
    );
    on("warning", (d) => push("warning", "warning", `${d.code}: ${d.msg}`));
    on("artifact", (d) => push("artifact", "info", `wrote ${d.path}`));
    on("lagged", (d) => push("lagged", "warning", String(d.message ?? "Some events were dropped.")));
    on("progress", (d) =>
      setProgress({
        done: Number(d.done ?? 0),
        bytes: Number(d.bytes ?? 0),
        unit: typeof d.unit === "string" ? d.unit : undefined,
      }),
    );
    on("status", (d) => {
      const value = String(d.status ?? "");
      if (value && value !== "running") {
        setStatus(value);
        finishedRef.current?.(value);
        source.close();
      }
    });

    source.onerror = () => {
      // EventSource retries on its own; only a closed connection is terminal.
      if (source.readyState === EventSource.CLOSED) {
        push("lagged", "warning", "Connection closed. Reload to see the full log.");
      }
    };

    return () => source.close();
  }, [jobId]);

  // Follow the tail, but stop fighting the user the moment they scroll up.
  useEffect(() => {
    const box = boxRef.current;
    if (box && pinnedRef.current) box.scrollTop = box.scrollHeight;
  }, [lines]);

  const tone = (level: string) =>
    level === "error"
      ? "text-danger"
      : level === "warning"
        ? "text-warn"
        : level === "debug"
          ? "text-muted"
          : "text-fg";

  return (
    <div className="card overflow-hidden">
      <div className="flex items-center justify-between border-b border-border px-4 py-2.5">
        <div className="flex items-center gap-2 text-sm">
          {status === "running" ? (
            <>
              <Spinner className="h-3.5 w-3.5 text-accent" />
              <span className="font-medium">Capturing…</span>
            </>
          ) : (
            <span className="font-medium">Finished — {status}</span>
          )}
        </div>
        {progress && (
          <span className="tabular-nums text-xs text-muted">
            {progress.done} {progress.unit ?? "URLs"} · {(progress.bytes / 1024 / 1024).toFixed(1)} MB
          </span>
        )}
      </div>

      <div
        ref={boxRef}
        onScroll={(event) => {
          const el = event.currentTarget;
          pinnedRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
        }}
        className="h-72 overflow-y-auto bg-raised/40 p-3 font-mono text-xs leading-5"
      >
        {lines.length === 0 ? (
          <p className="text-muted">Waiting for the engine to start…</p>
        ) : (
          lines.map((line) => (
            <div key={line.id} className={`whitespace-pre-wrap break-all ${tone(line.level)}`}>
              {line.text}
            </div>
          ))
        )}
      </div>
    </div>
  );
}
