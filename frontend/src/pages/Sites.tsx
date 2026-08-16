import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { filterFromParams, flatten, SiteFilters } from "../components/SiteFilters";
import { Alert, EmptyState, Field, Logo, Spinner } from "../components/ui";
import type { SiteFilter } from "../lib/api";
import { ApiError, endpoints, filterToQuery } from "../lib/api";
import { bytes, relative } from "../lib/format";

/**
 * The filter lives in the URL.
 *
 * Three things fall out of that and none of them are free otherwise: a filter
 * is linkable, the Folders page can link straight into one, and a saved view
 * is nothing more than the query string — the same string the server parses,
 * so there is no second definition of what a filter means.
 */
export default function Sites() {
  const client = useQueryClient();
  const [params, setParams] = useSearchParams();
  const [adding, setAdding] = useState(false);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [message, setMessage] = useState<string | null>(null);

  const filter = useMemo(() => filterFromParams(params), [params]);
  const queryString = filterToQuery(filter);

  const sites = useQuery({
    queryKey: ["sites", queryString],
    queryFn: () => endpoints.filterSites(filter),
  });
  const views = useQuery({ queryKey: ["views"], queryFn: endpoints.views });

  const apply = (next: SiteFilter) => {
    setSelected(new Set());
    setParams(new URLSearchParams(filterToQuery(next)), { replace: true });
  };

  const toggle = (id: number) =>
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const items = sites.data?.items ?? [];
  const filtered = Object.keys(filter).length > 0;
  // Space is reserved for a picture only once some site on this page has one,
  // so an archive captured before thumbnails existed keeps the rows it had
  // rather than growing a column of empty boxes.
  const anyThumbnails = items.some((site) => site.has_thumbnail);

  return (
    <div className="space-y-5">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold">Sites</h1>
          <p className="mt-1 text-sm text-muted">
            {sites.data
              ? `${sites.data.total} ${filtered ? "matching" : "archived"}`
              : " "}
          </p>
        </div>
        <button className="btn-primary" onClick={() => setAdding((v) => !v)}>
          {adding ? "Cancel" : "Add a site"}
        </button>
      </header>

      {message && <Alert kind="info">{message}</Alert>}
      {adding && <AddSite onDone={() => setAdding(false)} />}

      <SavedViews
        current={filter}
        active={queryString}
        views={views.data ?? []}
        onApply={apply}
        onChanged={() => client.invalidateQueries({ queryKey: ["views"] })}
      />

      <SiteFilters value={filter} onChange={apply} />

      {selected.size > 0 && (
        <BulkBar
          ids={[...selected]}
          onDone={async (summary) => {
            setMessage(summary);
            setSelected(new Set());
            await client.invalidateQueries({ queryKey: ["sites"] });
            await client.invalidateQueries({ queryKey: ["tags"] });
            await client.invalidateQueries({ queryKey: ["folders"] });
          }}
          onClear={() => setSelected(new Set())}
        />
      )}

      {sites.isLoading ? (
        <Spinner className="h-5 w-5 text-muted" />
      ) : items.length > 0 ? (
        <>
          <label className="flex items-center gap-2 px-1 text-xs text-muted">
            <input
              type="checkbox"
              checked={selected.size === items.length}
              onChange={(event) =>
                setSelected(event.target.checked ? new Set(items.map((s) => s.id)) : new Set())
              }
            />
            Select all {items.length} on this page
          </label>
          <div className="grid gap-3">
            {items.map((site) => (
              <div
                key={site.id}
                className={`card flex flex-wrap items-center gap-4 p-4 transition-colors ${
                  selected.has(site.id) ? "ring-1 ring-accent" : "hover:bg-raised"
                }`}
              >
                <input
                  type="checkbox"
                  className="shrink-0"
                  checked={selected.has(site.id)}
                  onChange={() => toggle(site.id)}
                  aria-label={`Select ${site.title}`}
                />
                {anyThumbnails && (
                  // Not a link: the title beside it already goes to the site,
                  // and a second focusable copy of the same destination is one
                  // more tab stop between the checkbox and anything useful.
                  <div className="shrink-0 overflow-hidden rounded border border-border bg-raised">
                    {site.has_thumbnail ? (
                      <img
                        src={endpoints.thumbnailUrl(site.id)}
                        alt=""
                        width={80}
                        height={50}
                        loading="lazy"
                        className="h-[50px] w-20 object-cover object-top"
                        // The row was laid out before the request was made, so
                        // a picture deleted since the list was fetched must
                        // leave the space rather than a broken-image icon.
                        onError={(event) => {
                          event.currentTarget.style.visibility = "hidden";
                        }}
                      />
                    ) : (
                      <span className="block h-[50px] w-20" />
                    )}
                  </div>
                )}
                <Link to={`/sites/${site.id}`} className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="truncate font-medium">{site.title}</span>
                    <StatusPill status={site.status} />
                    {site.tags.map((tag) => (
                      <span
                        key={tag}
                        className="rounded-full bg-raised px-2 py-0.5 text-[10px] text-muted"
                      >
                        {tag}
                      </span>
                    ))}
                  </div>
                  <p className="truncate text-xs text-muted">{site.seed_url}</p>
                  <p className="truncate font-mono text-[11px] text-muted opacity-70">
                    {site.folder_path}
                  </p>
                </Link>
                <dl className="flex shrink-0 gap-6 text-right text-xs">
                  <div>
                    <dt className="text-muted">URLs</dt>
                    <dd className="tabular-nums">{site.url_count.toLocaleString()}</dd>
                  </div>
                  <div>
                    <dt className="text-muted">Size</dt>
                    <dd className="tabular-nums">{bytes(site.size_bytes)}</dd>
                  </div>
                  <div className="w-20">
                    <dt className="text-muted">Captured</dt>
                    <dd>{relative(site.last_capture_at)}</dd>
                  </div>
                </dl>
              </div>
            ))}
          </div>
        </>
      ) : filtered ? (
        <EmptyState
          title="Nothing matches that filter"
          action={
            <button className="btn-ghost" onClick={() => apply({})}>
              Clear the filter
            </button>
          }
        />
      ) : (
        <EmptyState
          icon={<Logo className="h-10 w-10" />}
          title="No sites yet"
          action={
            <button className="btn-primary" onClick={() => setAdding(true)}>
              Add a site
            </button>
          }
        >
          Point Cairn at a URL and it will index the domains it touches, then crawl the ones
          you tick to WARC.
        </EmptyState>
      )}
    </div>
  );
}

function SavedViews({
  current,
  active,
  views,
  onApply,
  onChanged,
}: {
  current: SiteFilter;
  active: string;
  views: { id: number; name: string; query: SiteFilter; query_string: string; pinned: boolean }[];
  onApply: (filter: SiteFilter) => void;
  onChanged: () => void;
}) {
  const [naming, setNaming] = useState(false);
  const [name, setName] = useState("");
  const save = useMutation({
    mutationFn: () => endpoints.createView({ name, query: current, pinned: true }),
    onSuccess: () => {
      setNaming(false);
      setName("");
      onChanged();
    },
  });

  const savable = Object.keys(current).length > 0;
  if (!views.length && !savable) return null;

  return (
    <div className="flex flex-wrap items-center gap-2 text-sm">
      <button
        className={`rounded-md px-2.5 py-1 ${!active ? "bg-raised font-medium" : "text-muted"}`}
        onClick={() => onApply({})}
      >
        All sites
      </button>
      {views.map((view) => (
        <span key={view.id} className="group flex items-center">
          <button
            className={`rounded-md px-2.5 py-1 ${
              active === view.query_string ? "bg-raised font-medium" : "text-muted"
            }`}
            onClick={() => onApply(view.query)}
          >
            {view.name}
          </button>
          <button
            className="ml-0.5 hidden text-xs text-muted hover:text-danger group-hover:inline"
            title={`Delete the ${view.name} view`}
            onClick={async () => {
              await endpoints.deleteView(view.id);
              onChanged();
            }}
          >
            ×
          </button>
        </span>
      ))}

      {savable &&
        (naming ? (
          <form
            className="flex items-center gap-1.5"
            onSubmit={(event) => {
              event.preventDefault();
              save.mutate();
            }}
          >
            <input
              className="field w-40 py-1 text-xs"
              placeholder="Name this view"
              value={name}
              onChange={(event) => setName(event.target.value)}
              autoFocus
              required
            />
            <button className="btn-primary text-xs" disabled={save.isPending}>
              Save
            </button>
            <button type="button" className="btn-ghost text-xs" onClick={() => setNaming(false)}>
              Cancel
            </button>
          </form>
        ) : (
          <button className="btn-ghost text-xs" onClick={() => setNaming(true)}>
            + Save this filter
          </button>
        ))}
      {save.error && <span className="text-xs text-danger">{(save.error as ApiError).message}</span>}
    </div>
  );
}

function BulkBar({
  ids,
  onDone,
  onClear,
}: {
  ids: number[];
  onDone: (summary: string) => Promise<void>;
  onClear: () => void;
}) {
  const folders = useQuery({ queryKey: ["folders"], queryFn: endpoints.folders });
  const [tag, setTag] = useState("");
  const [error, setError] = useState<string | null>(null);

  const run = async (body: Parameters<typeof endpoints.bulkSites>[0], verb: string) => {
    setError(null);
    try {
      const result = await endpoints.bulkSites(body);
      const parts = [verb];
      if (result.queued_job_ids.length) {
        parts.push(
          `${result.queued_job_ids.length} archive(s) are being copied to a different ` +
            "filesystem — watch them under Jobs",
        );
      }
      if (result.skipped.length) parts.push(`skipped: ${result.skipped.join("; ")}`);
      await onDone(parts.join(". "));
    } catch (err) {
      setError((err as ApiError).message);
    }
  };

  return (
    <div className="card space-y-2 border-accent/40 bg-accent/5 p-3">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <span className="font-medium">{ids.length} selected</span>

        <input
          className="field w-40 py-1 text-xs"
          placeholder="tag name"
          value={tag}
          onChange={(event) => setTag(event.target.value)}
          aria-label="Tag to add or remove"
        />
        <button
          className="btn-ghost text-xs"
          disabled={!tag.trim()}
          onClick={() => void run({ site_ids: ids, add_tags: [tag] }, `Tagged ${ids.length} site(s)`)}
        >
          Add tag
        </button>
        <button
          className="btn-ghost text-xs"
          disabled={!tag.trim()}
          onClick={() =>
            void run({ site_ids: ids, remove_tags: [tag] }, `Untagged ${ids.length} site(s)`)
          }
        >
          Remove tag
        </button>

        <select
          className="field w-40 py-1 text-xs"
          value=""
          aria-label="Move selected sites to a folder"
          onChange={(event) => {
            if (!event.target.value) return;
            void run(
              { site_ids: ids, folder_id: Number(event.target.value) },
              `Moved ${ids.length} site(s)`,
            );
          }}
        >
          <option value="">Move to…</option>
          {flatten(folders.data ?? []).map((f) => (
            <option key={f.id} value={f.id}>
              {"— ".repeat(f.depth)}
              {f.name}
            </option>
          ))}
        </select>

        <button className="btn-ghost ml-auto text-xs" onClick={onClear}>
          Clear selection
        </button>
      </div>
      {error && <Alert kind="error">{error}</Alert>}
    </div>
  );
}

export function StatusPill({ status }: { status: string }) {
  const tone =
    {
      ready: "bg-ok/15 text-ok",
      ok: "bg-ok/15 text-ok",
      capturing: "bg-accent/15 text-accent",
      running: "bg-accent/15 text-accent",
      queued: "bg-accent/15 text-accent",
      partial: "bg-warn/15 text-warn",
      error: "bg-danger/15 text-danger",
      failed: "bg-danger/15 text-danger",
      cancelled: "bg-raised text-muted",
      // Accent rather than a warning tone: a paused crawl is waiting, not
      // damaged, and it is the one stopped state with something still to do.
      paused: "bg-accent/15 text-accent",
      interrupted: "bg-warn/15 text-warn",
      archived: "bg-raised text-muted",
    }[status] ?? "bg-raised text-muted";
  return (
    <span className={`rounded px-1.5 py-0.5 text-[10px] font-medium uppercase ${tone}`}>
      {status}
    </span>
  );
}

function AddSite({ onDone }: { onDone: () => void }) {
  const client = useQueryClient();
  const [seedUrl, setSeedUrl] = useState("");
  const [title, setTitle] = useState("");
  const [folderId, setFolderId] = useState<string>("");
  const [tags, setTags] = useState("");
  const [profileId, setProfileId] = useState<string>("");
  const profiles = useQuery({ queryKey: ["profiles"], queryFn: endpoints.profiles });
  const folders = useQuery({ queryKey: ["folders"], queryFn: endpoints.folders });

  const create = useMutation({
    mutationFn: () =>
      endpoints.createSite({
        seed_url: seedUrl,
        title: title || undefined,
        folder_id: folderId ? Number(folderId) : undefined,
        tags: tags
          .split(",")
          .map((t) => t.trim())
          .filter(Boolean),
        profile_id: profileId ? Number(profileId) : null,
      }),
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey: ["sites"] });
      await client.invalidateQueries({ queryKey: ["tags"] });
      await client.invalidateQueries({ queryKey: ["folders"] });
      onDone();
    },
  });

  return (
    <form
      className="card space-y-4 p-5"
      onSubmit={(event) => {
        event.preventDefault();
        create.mutate();
      }}
    >
      {create.error && <Alert kind="error">{(create.error as ApiError).message}</Alert>}

      <div className="grid gap-4 sm:grid-cols-2">
        <Field label="Address" htmlFor="seed" hint="A bare hostname becomes https.">
          <input
            id="seed"
            className="field"
            placeholder="example.blogspot.com"
            value={seedUrl}
            onChange={(event) => setSeedUrl(event.target.value)}
            autoFocus
            required
          />
        </Field>
        <Field label="Title" htmlFor="title" hint="Optional — defaults to the hostname.">
          <input
            id="title"
            className="field"
            value={title}
            onChange={(event) => setTitle(event.target.value)}
          />
        </Field>
        <Field label="Folder" htmlFor="folder" hint="Where the archive directory lives on disk.">
          <select
            id="folder"
            className="field"
            value={folderId}
            onChange={(event) => setFolderId(event.target.value)}
          >
            <option value="">Default</option>
            {flatten(folders.data ?? []).map((f) => (
              <option key={f.id} value={f.id}>
                {"— ".repeat(f.depth)}
                {f.name}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Tags" htmlFor="tags" hint="Comma separated. These become /data/by-tag links.">
          <input
            id="tags"
            className="field"
            placeholder="travel, photography"
            value={tags}
            onChange={(event) => setTags(event.target.value)}
          />
        </Field>
      </div>

      <Field
        label="Access profile"
        htmlFor="profile"
        hint="Needed for blogs behind a content warning. Manage these under Access profiles."
      >
        <select
          id="profile"
          className="field"
          value={profileId}
          onChange={(event) => setProfileId(event.target.value)}
        >
          <option value="">None</option>
          {profiles.data?.map((profile) => (
            <option key={profile.id} value={profile.id}>
              {profile.name}
              {profile.has_material ? "" : " (no cookies stored)"}
            </option>
          ))}
        </select>
      </Field>

      <div className="flex gap-2">
        <button className="btn-primary" disabled={create.isPending || !seedUrl}>
          {create.isPending && <Spinner />}
          Add site
        </button>
        <button type="button" className="btn-ghost" onClick={onDone}>
          Cancel
        </button>
      </div>
    </form>
  );
}
