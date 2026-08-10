import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRef, useState } from "react";

import { Alert, EmptyState, Field, Spinner } from "../components/ui";
import { ApiError, type CookieReport, type Profile, endpoints } from "../lib/api";
import { dateTime, relative } from "../lib/format";

export default function Profiles() {
  const [adding, setAdding] = useState(false);
  const profiles = useQuery({ queryKey: ["profiles"], queryFn: endpoints.profiles });

  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold">Access profiles</h1>
          <p className="mt-1 text-sm text-muted">
            Cookies that get a crawl past a content warning or a login.
          </p>
        </div>
        <button className="btn-primary" onClick={() => setAdding((v) => !v)}>
          {adding ? "Cancel" : "New profile"}
        </button>
      </header>

      <Alert kind="info" title="How to get a cookies.txt">
        Open the blog in your browser, click through the content warning, then export
        cookies with any <em>cookies.txt</em> extension — with <strong>session cookies
        included</strong>. Blogger's bypass usually is one, and most exporters drop them
        by default.
      </Alert>

      {adding && <NewProfile onDone={() => setAdding(false)} />}

      {profiles.isLoading ? (
        <Spinner className="h-5 w-5 text-muted" />
      ) : profiles.data && profiles.data.length > 0 ? (
        <div className="grid gap-3">
          {profiles.data.map((profile) => (
            <ProfileCard key={profile.id} profile={profile} />
          ))}
        </div>
      ) : (
        <EmptyState title="No access profiles">
          Sites that are not behind a warning do not need one.
        </EmptyState>
      )}
    </div>
  );
}

function NewProfile({ onDone }: { onDone: () => void }) {
  const client = useQueryClient();
  const [name, setName] = useState("");
  const [userAgent, setUserAgent] = useState("");

  const create = useMutation({
    mutationFn: () =>
      endpoints.createProfile({
        name,
        mode: "cookies",
        user_agent: userAgent || undefined,
      }),
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey: ["profiles"] });
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
      <Field label="Name" htmlFor="name">
        <input
          id="name"
          className="field"
          placeholder="blogger-interstitial"
          value={name}
          onChange={(event) => setName(event.target.value)}
          autoFocus
          required
        />
      </Field>
      <Field
        label="User agent"
        htmlFor="ua"
        hint="Optional. Match the browser you exported the cookies from — some bypasses bind the cookie to it."
      >
        <input
          id="ua"
          className="field"
          value={userAgent}
          onChange={(event) => setUserAgent(event.target.value)}
        />
      </Field>
      <button className="btn-primary" disabled={create.isPending || !name}>
        {create.isPending && <Spinner />}
        Create
      </button>
    </form>
  );
}

function ProfileCard({ profile }: { profile: Profile }) {
  const client = useQueryClient();
  const fileRef = useRef<HTMLInputElement>(null);
  const [report, setReport] = useState<CookieReport | null>(null);

  const upload = useMutation({
    mutationFn: (file: File) => endpoints.uploadCookies(profile.id, file),
    onSuccess: async (result) => {
      setReport(result);
      await client.invalidateQueries({ queryKey: ["profiles"] });
    },
    onError: (error) => {
      // The parse report is the useful part of a rejection: it names the line.
      const detail = (error as ApiError).detail as CookieReport | undefined;
      if (detail?.errors) setReport(detail);
    },
  });

  const clear = useMutation({
    mutationFn: () => endpoints.clearMaterial(profile.id),
    onSuccess: () => {
      setReport(null);
      void client.invalidateQueries({ queryKey: ["profiles"] });
    },
  });

  const remove = useMutation({
    mutationFn: () => endpoints.deleteProfile(profile.id),
    onSuccess: () => client.invalidateQueries({ queryKey: ["profiles"] }),
  });

  const expiringSoon =
    profile.expires_at != null &&
    new Date(profile.expires_at).getTime() - Date.now() < 7 * 86_400_000;

  return (
    <div className="card space-y-4 p-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2">
            <span className="font-medium">{profile.name}</span>
            <span className="rounded bg-raised px-1.5 py-0.5 text-[10px] uppercase text-muted">
              {profile.mode}
            </span>
          </div>
          <p className="mt-0.5 text-xs text-muted">
            {profile.has_material
              ? `${profile.cookie_count} cookies · ${profile.session_cookie_count} session`
              : "No cookies stored yet"}
            {profile.expires_at && ` · earliest expiry ${relative(profile.expires_at)}`}
          </p>
        </div>
        <div className="flex gap-2">
          <input
            ref={fileRef}
            type="file"
            accept=".txt,text/plain"
            className="hidden"
            onChange={(event) => {
              const file = event.target.files?.[0];
              if (file) upload.mutate(file);
              event.target.value = "";
            }}
          />
          <button className="btn-ghost" onClick={() => fileRef.current?.click()}>
            {upload.isPending && <Spinner />}
            {profile.has_material ? "Replace cookies" : "Upload cookies.txt"}
          </button>
          {profile.has_material && (
            <button className="btn-ghost" onClick={() => clear.mutate()}>
              Clear
            </button>
          )}
          <button
            className="btn-ghost text-danger"
            onClick={() => {
              if (confirm(`Delete profile "${profile.name}"?`)) remove.mutate();
            }}
          >
            Delete
          </button>
        </div>
      </div>

      {remove.error && <Alert kind="error">{(remove.error as ApiError).message}</Alert>}

      {profile.has_material && (
        <div className="flex flex-wrap gap-1.5">
          {profile.hosts_covered.map((host) => (
            <span key={host} className="rounded bg-raised px-2 py-0.5 font-mono text-[11px]">
              {host}
            </span>
          ))}
        </div>
      )}

      {expiringSoon && (
        <Alert kind="warn">
          The earliest cookie expires {relative(profile.expires_at)}. A long capture may lose
          access partway through — re-export before starting one.
        </Alert>
      )}

      {(report?.errors.length ?? 0) > 0 && (
        <Alert kind="error" title="That file could not be used">
          <ul className="list-disc space-y-1 pl-5">
            {report?.errors.map((error) => (
              <li key={error}>{error}</li>
            ))}
          </ul>
        </Alert>
      )}

      {(report?.warnings.length ?? profile.warnings.length) > 0 && (
        <Alert kind="warn" title="Worth checking">
          <ul className="list-disc space-y-1 pl-5">
            {(report?.warnings ?? profile.warnings).map((warning) => (
              <li key={warning}>{warning}</li>
            ))}
          </ul>
        </Alert>
      )}

      {profile.fingerprint && (
        <p className="hint font-mono">
          {profile.fingerprint} · stored {dateTime(profile.minted_at)}
        </p>
      )}
    </div>
  );
}
