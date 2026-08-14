import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import {
  ApiError,
  endpoints,
  type MediaItem,
  type MediaPolicy,
  type MediaSettings,
} from "../lib/api";
import { bytes } from "../lib/format";
import { Alert, Spinner } from "./ui";

const MB = 1024 * 1024;
const GB = 1024 * MB;

/**
 * Embedded video and audio: whether to fetch it, and what came back.
 *
 * The downloader has been in the post-processing chain since M9 and there was
 * no way to switch it on — the policy lives in the site's scope settings and
 * nothing wrote it, so the feature existed and was unreachable. This is the
 * screen that reaches it.
 *
 * Closed by default, and it says the disk cost before it says anything else. A
 * blog's text and images are megabytes; its embedded video is gigabytes, and
 * this is the one setting here that can quietly fill a disk overnight on a
 * schedule somebody set months ago.
 */
export function Media({ siteId }: { siteId: number }) {
  const [open, setOpen] = useState(false);
  const client = useQueryClient();
  const query = useQuery({
    queryKey: ["media", siteId],
    queryFn: () => endpoints.media(siteId),
    enabled: open,
  });

  const save = useMutation({
    mutationFn: (body: Record<string, unknown>) => endpoints.putMedia(siteId, body),
    onSuccess: (data) => client.setQueryData(["media", siteId], data),
  });

  const data: MediaSettings | undefined = query.data;

  return (
    <section className="card p-5">
      <button
        className="flex w-full items-center justify-between text-left"
        onClick={() => setOpen((v) => !v)}
      >
        <div>
          <h2 className="text-sm font-medium">Embedded video and audio</h2>
          <p className="hint mt-0.5">
            Neither crawler captures a video stream, so an archived post with a YouTube embed
            keeps a dead rectangle. Off by default, because video is the one thing here that can
            fill a disk.
          </p>
        </div>
        <span className="text-xs text-muted">{open ? "Hide" : "Show"}</span>
      </button>

      {open && (
        <div className="mt-4 space-y-4">
          {query.isLoading && <Spinner className="h-5 w-5 text-muted" />}
          {query.error && <Alert kind="error">{(query.error as ApiError).message}</Alert>}

          {data && !data.available && (
            <Alert kind="warn">
              {data.unavailable_reason || "yt-dlp is not available."} The setting below can still
              be changed, and captures will record that they could not fetch anything.
            </Alert>
          )}

          {data && (
            <>
              <MediaPolicyFields
                policy={data.policy}
                hosts={data.hosts}
                pending={save.isPending}
                onCommit={(body) => save.mutate(body)}
                scope="site"
              />
              {save.error && <Alert kind="error">{(save.error as ApiError).message}</Alert>}
              {Object.keys(data.override).length > 0 && (
                <button
                  className="btn-ghost text-xs"
                  disabled={save.isPending}
                  onClick={() => save.mutate({})}
                >
                  Use the instance default instead
                </button>
              )}
              <Collected siteId={siteId} data={data} />
            </>
          )}
        </div>
      )}
    </section>
  );
}

/**
 * The policy controls, shared by the per-site section and the instance-wide
 * default in Settings. One component because they are the same six fields with
 * the same bounds, and two copies would drift the moment one gained a seventh.
 */
export function MediaPolicyFields({
  policy,
  hosts,
  onCommit,
  pending,
  scope,
}: {
  policy: Required<MediaPolicy>;
  hosts: string[];
  onCommit: (body: Record<string, unknown>) => void;
  pending: boolean;
  scope: "site" | "instance";
}) {
  // Every write sends the whole merged policy, so toggling one control cannot
  // silently drop a limit that was inherited rather than typed here.
  const commit = (patch: Record<string, unknown>) => onCommit({ ...policy, ...patch });

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-end gap-4">
        <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={policy.enabled}
            disabled={pending}
            onChange={(e) => commit({ enabled: e.target.checked })}
          />
          {scope === "site"
            ? "Download embedded media for this site"
            : "Download embedded media by default"}
        </label>
        {pending && <Spinner className="h-4 w-4 text-muted" />}
      </div>

      <div className="flex flex-wrap items-end gap-3">
        <SizeField
          label="Largest single item"
          value={policy.max_item_bytes}
          onCommit={(v) => commit({ max_item_bytes: v })}
        />
        <SizeField
          label="Budget per capture"
          value={policy.max_total_bytes}
          onCommit={(v) => commit({ max_total_bytes: v })}
        />
        <NumberField
          label="Most items per capture"
          value={policy.max_items}
          onCommit={(v) => commit({ max_items: v })}
        />
      </div>

      <p className="hint">
        All three apply at once, because any one of them alone has an obvious way to be exceeded.
        Anything refused is recorded with the reason rather than skipped quietly &mdash; &ldquo;the
        video is not here&rdquo; is worth finding out now rather than in five years.
      </p>

      <details>
        <summary className="cursor-pointer text-xs text-muted">Advanced</summary>
        <div className="mt-3 space-y-3">
          <label className="block text-xs text-muted">
            <span className="mb-1 block">Format</span>
            <TextCommit
              value={policy.format}
              onCommit={(v) => commit({ format: v })}
              className="input w-full max-w-md font-mono text-xs"
            />
            <span className="hint mt-1 block">
              A yt-dlp format selector. The default asks for a single file that needs no
              merging, because the image ships without ffmpeg on purpose &mdash; a format that
              needs merging fails with yt-dlp saying so.
            </span>
          </label>

          <label className="flex items-start gap-2 text-sm">
            <input
              type="checkbox"
              className="mt-0.5"
              checked={policy.allow_private_hosts}
              onChange={(e) => commit({ allow_private_hosts: e.target.checked })}
            />
            <span>
              Allow private and loopback addresses
              <span className="hint mt-0.5 block">
                These URLs come out of archived HTML somebody else wrote, which makes them the
                one fetch target here that an outsider chooses. Leave this off unless you are
                archiving something on your own network.
              </span>
            </span>
          </label>

          <p className="hint">
            Embeds are followed only for: {hosts.join(", ")}. Direct{" "}
            <code>&lt;video&gt;</code> and <code>&lt;audio&gt;</code> files are always followed.
          </p>
        </div>
      </details>
    </div>
  );
}

function Collected({ siteId, data }: { siteId: number; data: MediaSettings }) {
  if (data.items.length === 0) {
    return (
      <p className="text-sm text-muted">
        {data.policy.enabled
          ? "Nothing yet. The next capture of this site will look for embeds."
          : "Nothing has been downloaded for this site."}
      </p>
    );
  }

  const downloaded = data.items.filter((i) => i.status === "downloaded");
  const refused = data.items.filter((i) => i.status !== "downloaded");

  return (
    <div className="space-y-3">
      <h3 className="text-xs font-medium uppercase tracking-wide text-muted">
        Collected &mdash; {downloaded.length} item(s), {bytes(data.total_bytes)}
      </h3>

      <div className="grid gap-2">
        {downloaded.map((item) => (
          <Downloaded key={`${item.capture_id}-${item.filename}`} siteId={siteId} item={item} />
        ))}
      </div>

      {refused.length > 0 && (
        <details>
          <summary className="cursor-pointer text-xs text-muted">
            {refused.length} not downloaded
          </summary>
          <table className="mt-2 w-full text-sm">
            <tbody className="divide-y divide-border">
              {refused.map((item, i) => (
                <tr key={`${item.capture_id}-${i}`}>
                  <td className="py-1.5 pr-3 font-mono text-[11px] break-all">{item.url}</td>
                  <td className="py-1.5 text-xs text-muted">{item.reason || item.status}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </details>
      )}
    </div>
  );
}

function Downloaded({ siteId, item }: { siteId: number; item: MediaItem }) {
  const [playing, setPlaying] = useState(false);
  const src = endpoints.mediaUrl(siteId, item.capture_dir, item.filename);
  const isAudio = /\.(mp3|m4a|ogg|opus|wav|flac)$/i.test(item.filename);

  return (
    <div className="rounded border border-border p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="min-w-0">
          <p className="truncate text-sm">{item.title || item.filename}</p>
          <p className="truncate font-mono text-[11px] text-muted">{item.url}</p>
        </div>
        <div className="flex shrink-0 items-center gap-3">
          <span className="text-xs tabular-nums text-muted">{bytes(item.bytes)}</span>
          {item.playable ? (
            <button className="btn-ghost text-xs" onClick={() => setPlaying((v) => !v)}>
              {playing ? "Close" : "Play"}
            </button>
          ) : (
            // Recorded by a capture, and no longer on disk — a retention sweep
            // or a hand-deletion. The record is still worth showing; a link
            // that 404s is not.
            <span className="text-xs text-muted" title="The file is no longer on disk">
              file gone
            </span>
          )}
        </div>
      </div>

      {playing &&
        (isAudio ? (
          <audio className="mt-3 w-full" controls preload="none" src={src} />
        ) : (
          <video className="mt-3 max-h-96 w-full rounded bg-black" controls preload="none" src={src} />
        ))}
    </div>
  );
}

function SizeField({
  label,
  value,
  onCommit,
}: {
  label: string;
  value: number;
  onCommit: (value: number) => void;
}) {
  // Edited in MB and stored in bytes. Nobody wants to type 268435456, and a
  // field that accepts bytes is a field where one extra digit is a gigabyte.
  const [draft, setDraft] = useState(String(Math.round(value / MB)));
  return (
    <label className="text-xs text-muted">
      <span className="mb-1 block">{label} (MB)</span>
      <input
        className="input w-32 text-sm"
        type="number"
        min={0}
        max={Math.round((64 * GB) / MB)}
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={() => {
          const next = Math.round(Number(draft) * MB);
          if (Number.isFinite(next) && next >= 0 && next !== value) onCommit(next);
        }}
      />
    </label>
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
        className="input w-32 text-sm"
        type="number"
        min={0}
        max={1000}
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

function TextCommit({
  value,
  onCommit,
  className,
}: {
  value: string;
  onCommit: (value: string) => void;
  className?: string;
}) {
  const [draft, setDraft] = useState(value);
  return (
    <input
      className={className}
      value={draft}
      onChange={(e) => setDraft(e.target.value)}
      onBlur={() => {
        const next = draft.trim();
        if (next && next !== value) onCommit(next);
      }}
    />
  );
}
