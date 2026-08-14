import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRef, useState } from "react";

import { InteractiveBrowser } from "../components/InteractiveBrowser";
import { Alert, EmptyState, Field, Spinner } from "../components/ui";
import {
  ApiError,
  type CookieReport,
  type InteractiveSession,
  type MintResult,
  type Profile,
  type VerifyResult,
  endpoints,
} from "../lib/api";
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
  const [mode, setMode] = useState("cookies");
  const [verifyUrl, setVerifyUrl] = useState("");

  const create = useMutation({
    mutationFn: () =>
      endpoints.createProfile({
        name,
        mode,
        verify_url: verifyUrl || undefined,
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
        label="How the cookies are obtained"
        htmlFor="mode"
        hint="Every mode ends in a cookie jar — this is only about where it comes from. The
              crawler never runs JavaScript, so a userscript runs once here instead."
      >
        <select
          id="mode"
          className="field"
          value={mode}
          onChange={(event) => setMode(event.target.value)}
        >
          <option value="cookies">Upload a cookies.txt I exported</option>
          <option value="userscript">Run a Tampermonkey userscript</option>
          <option value="interactive">Open a browser and let me sign in</option>
        </select>
      </Field>
      <Field
        label="Verify URL"
        htmlFor="verify"
        hint={
          mode === "cookies"
            ? "Optional. A page behind the gate, used by the Test button."
            : "A page behind the gate. The script runs against this, and Test checks it."
        }
      >
        <input
          id="verify"
          className="field"
          placeholder="https://example.blogspot.com/"
          value={verifyUrl}
          onChange={(event) => setVerifyUrl(event.target.value)}
          required={mode !== "cookies"}
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
  const scriptRef = useRef<HTMLInputElement>(null);
  const [report, setReport] = useState<CookieReport | null>(null);
  const [mintResult, setMintResult] = useState<MintResult | null>(null);
  const [verifyResult, setVerifyResult] = useState<VerifyResult | null>(null);
  const [session, setSession] = useState<InteractiveSession | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const refresh = () => client.invalidateQueries({ queryKey: ["profiles"] });

  const upload = useMutation({
    mutationFn: (file: File) => endpoints.uploadCookies(profile.id, file),
    onSuccess: async (result) => {
      setReport(result);
      await refresh();
    },
    onError: (error) => {
      // The parse report is the useful part of a rejection: it names the line.
      const detail = (error as ApiError).detail as CookieReport | undefined;
      if (detail?.errors) setReport(detail);
    },
  });

  const uploadScript = useMutation({
    mutationFn: (file: File) => endpoints.uploadScript(profile.id, file),
    onSuccess: refresh,
  });

  const mint = useMutation({
    mutationFn: () => endpoints.mintProfile(profile.id),
    onSuccess: async (result) => {
      setMintResult(result.result);
      setActionError(null);
      await refresh();
    },
    onError: (error) => setActionError((error as ApiError).message),
  });

  const verify = useMutation({
    mutationFn: () => endpoints.verifyProfile(profile.id),
    onSuccess: (result) => {
      setVerifyResult(result);
      setActionError(null);
    },
    onError: (error) => setActionError((error as ApiError).message),
  });

  const startSession = useMutation({
    mutationFn: () => endpoints.startInteractive(profile.id),
    onSuccess: (started) => {
      setSession(started);
      setActionError(null);
    },
    onError: (error) => setActionError((error as ApiError).message),
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
            {profile.has_cookies ? "Replace cookies" : "Upload cookies.txt"}
          </button>
          <input
            ref={scriptRef}
            type="file"
            accept=".js,.user.js,text/javascript"
            className="hidden"
            onChange={(event) => {
              const file = event.target.files?.[0];
              if (file) uploadScript.mutate(file);
              event.target.value = "";
            }}
          />
          <button className="btn-ghost" onClick={() => scriptRef.current?.click()}>
            {uploadScript.isPending && <Spinner />}
            {profile.has_script ? "Replace script" : "Upload userscript"}
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

      {actionError && <Alert kind="error">{actionError}</Alert>}

      {/*
        The gap nothing else in the app would show. A login kept in
        localStorage works everywhere a browser is involved — the mint, this
        page's test, browser-based discovery — and nowhere wget is, because
        wget is handed `--load-cookies` and nothing else. Without this line,
        "the test passes and the capture gets the sign-in page" has no
        explanation anywhere.
      */}
      {profile.storage_note && (
        <Alert kind="info" title="This profile is more than a cookie jar">
          {profile.storage_note}
        </Alert>
      )}

      {profile.script && (
        <div className="rounded-md border border-border p-3 text-xs">
          <p className="font-medium">
            {profile.script.name ?? "Userscript"}
            {profile.script.version && (
              <span className="ml-1 text-muted">v{profile.script.version}</span>
            )}
            <span className="ml-2 text-muted">runs at {profile.script.run_at}</span>
          </p>
          {profile.script.matches.length > 0 && (
            <p className="mt-1 font-mono text-muted">{profile.script.matches.join("  ")}</p>
          )}
          {profile.script.warnings.length > 0 && (
            <ul className="mt-2 list-disc space-y-1 pl-5 text-warn">
              {profile.script.warnings.map((warning) => (
                <li key={warning}>{warning}</li>
              ))}
            </ul>
          )}
        </div>
      )}

      <BrowserProfile profile={profile} />

      <VerifyUrl profile={profile} />

      {profile.has_script && !profile.verify_url && (
        <Alert kind="warn" title="Nothing to run the script against">
          A userscript is stored, but the mint needs a page to run it on. Set the verify URL
          above — a page behind the gate — and the button below will work.
        </Alert>
      )}

      {/* The actions that need a browser, and the one that does not. */}
      <div className="flex flex-wrap gap-2">
        {profile.has_script && (
          <button
            className="btn-ghost"
            onClick={() => mint.mutate()}
            disabled={mint.isPending || !profile.verify_url}
          >
            {mint.isPending && <Spinner />}
            Run the script now
          </button>
        )}
        {profile.has_cookies && profile.verify_url && (
          <button className="btn-ghost" onClick={() => verify.mutate()} disabled={verify.isPending}>
            {verify.isPending && <Spinner />}
            Test against {new URL(profile.verify_url).hostname}
          </button>
        )}
        {!session && (
          <button
            className="btn-ghost"
            onClick={() => startSession.mutate()}
            disabled={startSession.isPending}
          >
            {startSession.isPending && <Spinner />}
            Open a browser and sign in
          </button>
        )}
      </div>

      {mintResult && (
        <Alert kind={mintResult.ok ? "ok" : "error"} title={mintResult.ok ? "Minted" : "No jar"}>
          <p>{mintResult.reason}</p>
          {mintResult.console.length > 0 && (
            <pre className="mt-2 max-h-32 overflow-auto rounded bg-raised p-2 font-mono text-[11px]">
              {mintResult.console.join("\n")}
            </pre>
          )}
        </Alert>
      )}

      {verifyResult && (
        <Alert kind={verifyResult.ok ? "ok" : "warn"}>
          {verifyResult.ok ? "Real content: " : "Still gated: "}
          {verifyResult.reason}
        </Alert>
      )}

      {session && (
        <InteractiveBrowser
          session={session}
          onClosed={() => {
            setSession(null);
            void refresh();
          }}
        />
      )}

      {profile.fingerprint && (
        <p className="hint font-mono">
          {profile.fingerprint} · stored {dateTime(profile.minted_at)}
        </p>
      )}
    </div>
  );
}

/**
 * The browsertrix browser profile: the one credential this app cannot mint.
 *
 * browsertrix will not read a cookie jar, and it ignores a profile built by
 * our own Chromium because it runs Brave. The only thing it accepts is a
 * tarball from its own `create-login-profile` — which is also headful under
 * Xvfb, and therefore gets through sign-ins the in-app browser cannot.
 *
 * The instructions are here rather than in the docs because nothing in that
 * tool's own VNC window mentions the step that actually saves the profile.
 */
function BrowserProfile({ profile }: { profile: Profile }) {
  const client = useQueryClient();
  const fileRef = useRef<HTMLInputElement>(null);
  const [error, setError] = useState<string | null>(null);
  const [showHow, setShowHow] = useState(false);

  const refresh = () => client.invalidateQueries({ queryKey: ["profiles"] });

  const upload = useMutation({
    mutationFn: (file: File) => endpoints.uploadBrowserProfile(profile.id, file),
    onSuccess: async () => {
      setError(null);
      await refresh();
    },
    onError: (err) => setError((err as ApiError).message),
  });

  const remove = useMutation({
    mutationFn: () => endpoints.clearBrowserProfile(profile.id),
    onSuccess: refresh,
  });

  return (
    <div className="rounded-md border border-border p-3 text-xs">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <p className="font-medium">browsertrix browser profile</p>
          <p className="mt-0.5 text-muted">
            {profile.browser_profile
              ? `${megabytes(profile.browser_profile.size)} · ${profile.browser_profile.sha256} · stored ${relative(profile.browser_profile.stored_at)}`
              : "None. The browsertrix engine cannot use a cookie jar; this is what gets it past a login."}
          </p>
        </div>
        <div className="flex gap-2">
          <input
            ref={fileRef}
            type="file"
            accept=".gz,.tgz,application/gzip"
            className="hidden"
            onChange={(event) => {
              const file = event.target.files?.[0];
              if (file) upload.mutate(file);
              event.target.value = "";
            }}
          />
          <button className="btn-ghost" onClick={() => setShowHow((v) => !v)}>
            {showHow ? "Hide steps" : "How do I make one?"}
          </button>
          <button className="btn-ghost" onClick={() => fileRef.current?.click()}>
            {upload.isPending && <Spinner />}
            {profile.has_browser_profile ? "Replace" : "Upload profile.tar.gz"}
          </button>
          {profile.has_browser_profile && (
            <button className="btn-ghost" onClick={() => remove.mutate()}>
              Remove
            </button>
          )}
        </div>
      </div>

      {error && (
        <div className="mt-3">
          <Alert kind="error">{error}</Alert>
        </div>
      )}

      {showHow && (
        <div className="mt-3 space-y-2 border-t border-border pt-3">
          <p>
            Run the crawler&apos;s own profile browser, sign in, then commit it. It has to be
            that tool: it runs the same browser the crawl will, and it is headful under Xvfb,
            which is why a Google sign-in works there and not in the browser on this page.
          </p>
          <pre className="overflow-x-auto rounded bg-raised p-2 font-mono text-[11px]">
            {`docker run --rm -p 9223:9223 -p 6080:6080 \\
  --shm-size=2g -e VNC_PASS=changeme \\
  -v /mnt/user/appdata/cairn/btrix:/crawls \\
  webrecorder/browsertrix-crawler:1.14.1 \\
  create-login-profile --url "${profile.verify_url || "https://example.com/"}" --cookieDays 30`}
          </pre>
          <p>
            Open <code className="font-mono">http://NAS-IP:9223/vnc/?host=NAS-IP&amp;port=6080&amp;password=changeme</code>{" "}
            and sign in. Then commit it — nothing in that window does this for you, and closing
            the container without it loses the login:
          </p>
          <pre className="overflow-x-auto rounded bg-raised p-2 font-mono text-[11px]">
            {`curl -X POST -H "Content-Type: application/json" -d '{}' \\
  http://NAS-IP:9223/createProfile`}
          </pre>
          <p>
            Upload the <code className="font-mono">profiles/profile.tar.gz</code> it wrote. It is
            sealed here with everything else, and unsealed only into the job&apos;s temp
            directory while a capture runs.
          </p>
        </div>
      )}
    </div>
  );
}

function megabytes(bytes: number): string {
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/**
 * The verify URL, editable after the fact.
 *
 * It used to be settable only on the create form, which asks for it once and
 * only insists when the mode chosen *at that moment* needs one. The default
 * mode is `cookies`, so the ordinary path — make a profile, then upload a
 * userscript to it — produced a card offering "Run the script now" whose only
 * possible outcome was a 409 telling you to set a field nothing in the UI
 * offered. The same gap hid the Test button from every cookies profile made
 * without one.
 *
 * It is also the field people expect the interactive browser to set by
 * navigating, which it does not: that browser and the mint's are two different
 * browsers, and the mint reads this row rather than looking at what the other
 * one is showing.
 */
function VerifyUrl({ profile }: { profile: Profile }) {
  const client = useQueryClient();
  const [value, setValue] = useState(profile.verify_url ?? "");
  const dirty = value.trim() !== (profile.verify_url ?? "");

  const save = useMutation({
    mutationFn: () => endpoints.updateProfile(profile.id, { verify_url: value.trim() }),
    onSuccess: () => client.invalidateQueries({ queryKey: ["profiles"] }),
  });

  return (
    <div className="space-y-2">
      <Field
        label="Verify URL"
        htmlFor={`verify-${profile.id}`}
        hint="A page behind the gate. The userscript is run against this, and Test fetches it."
      >
        <div className="flex gap-2">
          <input
            id={`verify-${profile.id}`}
            className="field"
            placeholder="https://example.blogspot.com/"
            value={value}
            onChange={(event) => setValue(event.target.value)}
          />
          <button
            className="btn-ghost"
            disabled={!dirty || save.isPending}
            onClick={() => save.mutate()}
          >
            {save.isPending && <Spinner />}
            Save
          </button>
        </div>
      </Field>
      {save.error && <Alert kind="error">{(save.error as ApiError).message}</Alert>}
    </div>
  );
}
