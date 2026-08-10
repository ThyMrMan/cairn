import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";

import { Alert, EmptyState, Field, Logo, Spinner } from "../components/ui";
import { ApiError, endpoints } from "../lib/api";
import { bytes, relative } from "../lib/format";

export default function Sites() {
  const [adding, setAdding] = useState(false);
  const [search, setSearch] = useState("");
  const sites = useQuery({
    queryKey: ["sites", search],
    queryFn: () => endpoints.sites({ q: search || undefined }),
  });

  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold">Sites</h1>
          <p className="mt-1 text-sm text-muted">
            {sites.data ? `${sites.data.total} archived` : " "}
          </p>
        </div>
        <button className="btn-primary" onClick={() => setAdding((v) => !v)}>
          {adding ? "Cancel" : "Add a site"}
        </button>
      </header>

      {adding && <AddSite onDone={() => setAdding(false)} />}

      {(sites.data?.total ?? 0) > 0 && (
        <input
          className="field max-w-sm"
          placeholder="Filter by title, host or URL"
          value={search}
          onChange={(event) => setSearch(event.target.value)}
        />
      )}

      {sites.isLoading ? (
        <Spinner className="h-5 w-5 text-muted" />
      ) : sites.data && sites.data.items.length > 0 ? (
        <div className="grid gap-3">
          {sites.data.items.map((site) => (
            <Link
              key={site.id}
              to={`/sites/${site.id}`}
              className="card flex flex-wrap items-center gap-4 p-4 transition-colors hover:bg-raised"
            >
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2">
                  <span className="truncate font-medium">{site.title}</span>
                  <StatusPill status={site.status} />
                </div>
                <p className="truncate text-xs text-muted">{site.seed_url}</p>
              </div>
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
            </Link>
          ))}
        </div>
      ) : search ? (
        <EmptyState title="Nothing matches that filter" />
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
          Point Cairn at a URL and it will crawl that host to WARC. Discovery and the
          domain picker arrive in M2; for now a new site is scoped to its own host.
        </EmptyState>
      )}
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
      interrupted: "bg-warn/15 text-warn",
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
  const [profileId, setProfileId] = useState<string>("");
  const profiles = useQuery({ queryKey: ["profiles"], queryFn: endpoints.profiles });

  const create = useMutation({
    mutationFn: () =>
      endpoints.createSite({
        seed_url: seedUrl,
        title: title || undefined,
        profile_id: profileId ? Number(profileId) : null,
      }),
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey: ["sites"] });
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
      {create.error && (
        <Alert kind="error">{(create.error as ApiError).message}</Alert>
      )}

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
