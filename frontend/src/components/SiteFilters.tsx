import { useQuery } from "@tanstack/react-query";

import type { Folder, SiteFilter } from "../lib/api";
import { endpoints } from "../lib/api";

/**
 * The filter bar.
 *
 * The filter it produces is the same object the server reads and a saved view
 * stores — same field names, same values — so "save this as a view" is
 * literally handing this object over. Adding a control here means adding the
 * field to `SiteFilter` on both sides; anything else is silently ignored,
 * which is the failure docs/09 warns about.
 */
export function SiteFilters({
  value,
  onChange,
}: {
  value: SiteFilter;
  onChange: (next: SiteFilter) => void;
}) {
  const folders = useQuery({ queryKey: ["folders"], queryFn: endpoints.folders });
  const tags = useQuery({ queryKey: ["tags"], queryFn: endpoints.tags });
  const flat = flatten(folders.data ?? []);
  const set = (patch: SiteFilter) => onChange(prune({ ...value, ...patch }));

  const activeTags = value.tags ?? [];
  const toggleTag = (slug: string) =>
    set({
      tags: activeTags.includes(slug)
        ? activeTags.filter((t) => t !== slug)
        : [...activeTags, slug],
    });

  return (
    <div className="card space-y-3 p-4">
      <div className="flex flex-wrap gap-2">
        <input
          className="field min-w-48 flex-1"
          placeholder="Title, host, URL or notes"
          value={value.q ?? ""}
          onChange={(event) => set({ q: event.target.value || undefined })}
          aria-label="Search sites"
        />

        <select
          className="field w-44"
          value={value.folder_id ?? ""}
          aria-label="Folder"
          onChange={(event) =>
            set({ folder_id: event.target.value ? Number(event.target.value) : undefined })
          }
        >
          <option value="">Any folder</option>
          {flat.map((f) => (
            <option key={f.id} value={f.id}>
              {"— ".repeat(f.depth)}
              {f.name}
            </option>
          ))}
        </select>

        <select
          className="field w-36"
          value={value.status ?? ""}
          aria-label="Status"
          onChange={(event) => set({ status: event.target.value || undefined })}
        >
          <option value="">Any status</option>
          {["new", "indexed", "ready", "capturing", "error"].map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>

        <select
          className="field w-44"
          value={value.sort ?? "-updated_at"}
          aria-label="Sort by"
          onChange={(event) => set({ sort: event.target.value })}
        >
          <option value="-updated_at">Recently changed</option>
          <option value="title">Title A–Z</option>
          <option value="-last_capture_at">Recently captured</option>
          <option value="-size_bytes">Largest first</option>
          <option value="-url_count">Most URLs</option>
          <option value="created_at">Oldest first</option>
        </select>
      </div>

      <div className="flex flex-wrap items-center gap-x-4 gap-y-2 text-sm">
        {(tags.data?.length ?? 0) > 0 && (
          <div className="flex flex-wrap items-center gap-1.5">
            {tags.data?.map((tag) => (
              <button
                key={tag.id}
                type="button"
                aria-pressed={activeTags.includes(tag.slug)}
                onClick={() => toggleTag(tag.slug)}
                className={`rounded-full border px-2 py-0.5 text-xs transition-colors ${
                  activeTags.includes(tag.slug)
                    ? "border-accent bg-accent/15 text-accent"
                    : "border-border text-muted hover:text-fg"
                }`}
                style={
                  tag.color && activeTags.includes(tag.slug)
                    ? { borderColor: tag.color, color: tag.color }
                    : undefined
                }
              >
                {tag.name}
                <span className="ml-1 opacity-60">{tag.site_count}</span>
              </button>
            ))}
            {activeTags.length > 1 && (
              <select
                className="field w-28 py-0.5 text-xs"
                value={value.tag_mode ?? "all"}
                aria-label="Match tags"
                onChange={(event) =>
                  set({ tag_mode: event.target.value === "any" ? "any" : "all" })
                }
              >
                <option value="all">all of these</option>
                <option value="any">any of these</option>
              </select>
            )}
          </div>
        )}

        <label className="flex items-center gap-1.5 text-muted">
          <input
            type="checkbox"
            checked={value.has_errors === true}
            onChange={(event) => set({ has_errors: event.target.checked || undefined })}
          />
          Has capture errors
        </label>
        <label className="flex items-center gap-1.5 text-muted">
          <input
            type="checkbox"
            checked={value.never_captured === true}
            onChange={(event) => set({ never_captured: event.target.checked || undefined })}
          />
          Never captured
        </label>
        {value.folder_id != null && (
          <label className="flex items-center gap-1.5 text-muted">
            <input
              type="checkbox"
              checked={value.folder_recursive !== false}
              onChange={(event) => set({ folder_recursive: event.target.checked })}
            />
            Include subfolders
          </label>
        )}
      </div>
    </div>
  );
}

/** Drop anything at its default, so the filter serializes to the shortest
 *  query that means the same thing — and matches what the server stores. */
export function prune(filter: SiteFilter): SiteFilter {
  const out: SiteFilter = { ...filter };
  if (!out.tags?.length) delete out.tags;
  if (out.tag_mode === "all") delete out.tag_mode;
  if (out.folder_recursive === true) delete out.folder_recursive;
  if (out.sort === "-updated_at") delete out.sort;
  for (const key of Object.keys(out) as (keyof SiteFilter)[]) {
    const value = out[key];
    if (value === undefined || value === "" || value === false) delete out[key];
  }
  return out;
}

export function filterFromParams(params: URLSearchParams): SiteFilter {
  const filter: SiteFilter = {};
  const number = (key: "folder_id" | "profile_id" | "size_min" | "size_max") => {
    const raw = params.get(key);
    if (raw) filter[key] = Number(raw);
  };
  const text = (key: "status" | "engine_id" | "host" | "q" | "sort") => {
    const raw = params.get(key);
    if (raw) filter[key] = raw;
  };

  number("folder_id");
  number("profile_id");
  number("size_min");
  number("size_max");
  text("status");
  text("engine_id");
  text("host");
  text("q");
  text("sort");

  const tags = params.getAll("tag");
  if (tags.length) filter.tags = tags;
  if (params.get("tag_mode") === "any") filter.tag_mode = "any";
  if (params.get("folder_recursive") === "false") filter.folder_recursive = false;
  if (params.get("has_errors") === "true") filter.has_errors = true;
  if (params.get("never_captured") === "true") filter.never_captured = true;
  const after = params.get("last_capture_after");
  if (after) filter.last_capture_after = after;
  const before = params.get("last_capture_before");
  if (before) filter.last_capture_before = before;
  return filter;
}

export function flatten(
  nodes: Folder[],
  depth = 0,
): { id: number; name: string; path: string; depth: number }[] {
  return nodes.flatMap((node) => [
    { id: node.id, name: node.name, path: node.path, depth },
    ...flatten(node.children, depth + 1),
  ]);
}
