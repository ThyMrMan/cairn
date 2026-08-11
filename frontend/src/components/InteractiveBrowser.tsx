import { useCallback, useEffect, useRef, useState } from "react";

import type { InteractiveSession } from "../lib/api";
import { ApiError, endpoints } from "../lib/api";
import { Alert, Spinner } from "./ui";

/**
 * A real browser, streamed into a canvas.
 *
 * Frames arrive as binary WebSocket messages — JPEG, roughly 8 KB each — and
 * input goes back as JSON. Two things about that are worth knowing before
 * changing anything here:
 *
 * **Silence is normal.** Chromium only emits a frame when something on the
 * page actually changes, so a settled page sends nothing at all. The server
 * sends an explicit `idle` message so this component can tell "nothing is
 * moving" from "the connection died", because they look identical otherwise.
 *
 * **Coordinates are the page's, not the canvas's.** The canvas is scaled to
 * fit the panel, so every click has to be mapped back through that scale or
 * it lands somewhere else on the page — and the further from the top-left,
 * the further off it is, which makes it look like a flaky browser rather than
 * a units bug.
 */
export function InteractiveBrowser({
  session,
  onClosed,
}: {
  session: InteractiveSession;
  onClosed: () => void;
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const socketRef = useRef<WebSocket | null>(null);
  const [status, setStatus] = useState<"connecting" | "live" | "closed">("connecting");
  const [url, setUrl] = useState(session.url);
  const [typed, setTyped] = useState(session.url);
  const [error, setError] = useState<string | null>(null);

  const send = useCallback((message: Record<string, unknown>) => {
    const socket = socketRef.current;
    if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify(message));
  }, []);

  useEffect(() => {
    const socket = new WebSocket(endpoints.interactiveSocketUrl(session.profile_id, session.session_id));
    socket.binaryType = "blob";
    socketRef.current = socket;

    socket.onopen = () => setStatus("live");
    socket.onclose = () => setStatus("closed");
    socket.onerror = () => setError("The connection to the browser dropped.");

    socket.onmessage = async (event) => {
      if (typeof event.data !== "string") {
        const bitmap = await createImageBitmap(event.data as Blob);
        const canvas = canvasRef.current;
        const context = canvas?.getContext("2d");
        if (canvas && context) {
          canvas.width = bitmap.width;
          canvas.height = bitmap.height;
          context.drawImage(bitmap, 0, 0);
        }
        bitmap.close();
        return;
      }
      const message = JSON.parse(event.data) as { type: string; url?: string; message?: string };
      if (message.type === "where" && message.url) {
        setUrl(message.url);
        setTyped(message.url);
      } else if (message.type === "error" && message.message) {
        setError(message.message);
      }
    };

    return () => socket.close();
  }, [session.profile_id, session.session_id]);

  /** Canvas pixels → page pixels. See the note above. */
  const toPage = (event: React.MouseEvent<HTMLCanvasElement>) => {
    const canvas = canvasRef.current;
    if (!canvas) return { x: 0, y: 0 };
    const box = canvas.getBoundingClientRect();
    return {
      x: ((event.clientX - box.left) / box.width) * session.width,
      y: ((event.clientY - box.top) / box.height) * session.height,
    };
  };

  const onKeyDown = (event: React.KeyboardEvent) => {
    // Printable characters go as text: synthesising key events with the right
    // virtual key codes is fiddly and fails silently, leaving an empty field.
    if (event.key.length === 1 && !event.ctrlKey && !event.metaKey) {
      event.preventDefault();
      send({ type: "text", text: event.key });
      return;
    }
    const special = [
      "Enter", "Backspace", "Tab", "Delete", "ArrowLeft", "ArrowRight",
      "ArrowUp", "ArrowDown", "Home", "End", "PageUp", "PageDown", "Escape",
    ];
    if (special.includes(event.key)) {
      event.preventDefault();
      send({ type: "key", key: event.key });
    }
  };

  return (
    <div className="space-y-2">
      {error && <Alert kind="error">{error}</Alert>}

      <div className="flex flex-wrap items-center gap-1.5">
        <button className="btn-ghost px-2 text-xs" onClick={() => send({ type: "back" })}>
          ←
        </button>
        <button className="btn-ghost px-2 text-xs" onClick={() => send({ type: "forward" })}>
          →
        </button>
        <button className="btn-ghost px-2 text-xs" onClick={() => send({ type: "reload" })}>
          ⟳
        </button>
        <form
          className="flex min-w-0 flex-1 gap-1.5"
          onSubmit={(event) => {
            event.preventDefault();
            send({ type: "navigate", url: typed });
          }}
        >
          <input
            className="field min-w-0 flex-1 py-1 font-mono text-xs"
            value={typed}
            onChange={(event) => setTyped(event.target.value)}
            aria-label="Address in the remote browser"
          />
        </form>
        <span
          className={`rounded px-1.5 py-0.5 text-[10px] uppercase ${
            status === "live" ? "bg-ok/15 text-ok" : "bg-raised text-muted"
          }`}
        >
          {status}
        </span>
      </div>

      <div className="relative overflow-hidden rounded-md border border-border bg-raised">
        {status === "connecting" && (
          <div className="absolute inset-0 flex items-center justify-center">
            <Spinner className="h-5 w-5 text-muted" />
          </div>
        )}
        {/* tabIndex so the canvas can hold focus and receive keystrokes. */}
        <canvas
          ref={canvasRef}
          tabIndex={0}
          className="block w-full cursor-crosshair outline-none"
          style={{ aspectRatio: `${session.width} / ${session.height}` }}
          onMouseDown={(event) => {
            event.currentTarget.focus();
            send({ type: "mouse", action: "down", ...toPage(event), clickCount: event.detail || 1 });
          }}
          onMouseUp={(event) =>
            send({ type: "mouse", action: "up", ...toPage(event), clickCount: event.detail || 1 })
          }
          onMouseMove={(event) => send({ type: "mouse", action: "move", ...toPage(event) })}
          onWheel={(event) =>
            send({ type: "wheel", ...toPage(event), deltaX: event.deltaX, deltaY: event.deltaY })
          }
          onKeyDown={onKeyDown}
          onContextMenu={(event) => event.preventDefault()}
        />
      </div>

      <p className="hint">
        Click into the picture to give it your keyboard. Sign in or click through the warning
        as you normally would, then press Save below — nothing is stored until you do.
      </p>
      <p className="truncate font-mono text-[11px] text-muted">{url}</p>

      <div className="flex flex-wrap gap-2">
        <SaveButton profileId={session.profile_id} onSaved={onClosed} />
        <button
          className="btn-ghost"
          onClick={async () => {
            await endpoints.stopInteractive(session.profile_id);
            onClosed();
          }}
        >
          Discard and close
        </button>
      </div>
    </div>
  );
}

function SaveButton({ profileId, onSaved }: { profileId: number; onSaved: () => void }) {
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  return (
    <>
      <button
        className="btn-primary"
        disabled={busy}
        onClick={async () => {
          setBusy(true);
          setProblem(null);
          try {
            await endpoints.saveInteractive(profileId);
            await endpoints.stopInteractive(profileId);
            onSaved();
          } catch (err) {
            setProblem((err as ApiError).message);
          } finally {
            setBusy(false);
          }
        }}
      >
        {busy && <Spinner />}
        Save this session as the profile
      </button>
      {problem && <Alert kind="error">{problem}</Alert>}
    </>
  );
}
