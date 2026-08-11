import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";

import { Alert, Field, Spinner } from "../components/ui";
import { ApiError, endpoints } from "../lib/api";
import { useAuth } from "../lib/auth";
import { dateTime, relative } from "../lib/format";
import { useVersion } from "../lib/version";

export default function Settings() {
  return (
    <div className="space-y-8">
      <header>
        <h1 className="text-2xl font-semibold">Settings</h1>
        <p className="mt-1 text-sm text-muted">This instance, your account, and security.</p>
      </header>
      <AboutSection />
      <PasswordSection />
      <TotpSection />
      <SessionsSection />
      <AuditSection />
    </div>
  );
}

function AboutSection() {
  const version = useVersion();

  return (
    <Section
      title="About"
      description="Which build this instance is running. The version stays the same across
                  commits; the build id is what changes when you deploy."
    >
      <dl className="grid max-w-md grid-cols-[7rem_1fr] gap-y-2 text-sm">
        <dt className="text-muted">Version</dt>
        <dd className="font-mono">{version.data?.version ?? "—"}</dd>
        <dt className="text-muted">Build</dt>
        <dd className="break-all font-mono">{version.data?.build ?? "—"}</dd>
        <dt className="text-muted">Built</dt>
        <dd className="font-mono">
          {version.data?.built_at
            ? dateTime(version.data.built_at)
            : "running from a source checkout"}
        </dd>
      </dl>
    </Section>
  );
}

function Section({ title, description, children }: {
  title: string;
  description?: string;
  children: React.ReactNode;
}) {
  return (
    <section className="card p-5">
      <h2 className="text-base font-medium">{title}</h2>
      {description && <p className="mt-1 text-sm text-muted">{description}</p>}
      <div className="mt-4">{children}</div>
    </section>
  );
}

function PasswordSection() {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [result, setResult] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [problems, setProblems] = useState<string[]>([]);

  const mutation = useMutation({
    mutationFn: () => endpoints.changePassword(current, next),
    onSuccess: (data) => {
      setResult(
        data.revoked_sessions > 0
          ? `Password updated. ${data.revoked_sessions} other session(s) signed out.`
          : "Password updated.",
      );
      setError(null);
      setProblems([]);
      setCurrent("");
      setNext("");
    },
    onError: (err) => {
      setResult(null);
      setError(err instanceof ApiError ? err.message : "Something went wrong.");
      setProblems(err instanceof ApiError ? err.problems : []);
    },
  });

  function submit(e: FormEvent) {
    e.preventDefault();
    mutation.mutate();
  }

  return (
    <Section
      title="Password"
      description="Changing your password signs out every other session."
    >
      <form onSubmit={submit} className="max-w-sm space-y-4">
        {error && (
          <Alert kind="error" title={error}>
            {problems.length > 0 && (
              <ul className="list-inside list-disc">
                {problems.map((p) => <li key={p}>{p}</li>)}
              </ul>
            )}
          </Alert>
        )}
        {result && <Alert kind="ok">{result}</Alert>}

        <Field label="Current password" htmlFor="current">
          <input id="current" type="password" className="field" value={current}
                 onChange={(e) => setCurrent(e.target.value)}
                 autoComplete="current-password" required />
        </Field>
        <Field label="New password" htmlFor="new">
          <input id="new" type="password" className="field" value={next}
                 onChange={(e) => setNext(e.target.value)}
                 autoComplete="new-password" required />
        </Field>
        <button className="btn-primary" disabled={mutation.isPending}>
          {mutation.isPending && <Spinner />}
          Change password
        </button>
      </form>
    </Section>
  );
}

function TotpSection() {
  const { user, refresh } = useAuth();
  const [secret, setSecret] = useState<string | null>(null);
  const [uri, setUri] = useState<string | null>(null);
  const [code, setCode] = useState("");
  const [codes, setCodes] = useState<string[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const start = useMutation({
    mutationFn: endpoints.totpSetup,
    onSuccess: (d) => {
      setSecret(d.secret);
      setUri(d.provisioning_uri);
      setError(null);
    },
    onError: (e) => setError(e instanceof ApiError ? e.message : "Could not start setup."),
  });

  const confirm = useMutation({
    mutationFn: () => endpoints.totpConfirm(code),
    onSuccess: async (d) => {
      setCodes(d.recovery_codes);
      setSecret(null);
      setUri(null);
      setCode("");
      setError(null);
      await refresh();
    },
    onError: (e) => setError(e instanceof ApiError ? e.message : "Could not confirm."),
  });

  if (user?.totp_enabled && !codes) {
    return (
      <Section title="Two-factor authentication">
        <Alert kind="ok">Enabled. Codes from your authenticator app are required at sign-in.</Alert>
      </Section>
    );
  }

  return (
    <Section
      title="Two-factor authentication"
      description="Strongly recommended if this instance is reachable from the internet."
    >
      <div className="max-w-lg space-y-4">
        {error && <Alert kind="error">{error}</Alert>}

        {codes && (
          <Alert kind="warn" title="Save your recovery codes now">
            <p className="mb-2">
              Each works once, in place of a code from your app. This is the only time
              they are shown.
            </p>
            <ul className="grid grid-cols-2 gap-1 font-mono text-xs">
              {codes.map((c) => <li key={c}>{c}</li>)}
            </ul>
            <button className="btn-ghost mt-3" onClick={() => setCodes(null)}>
              I have saved them
            </button>
          </Alert>
        )}

        {!secret && !codes && (
          <button className="btn-primary" onClick={() => start.mutate()}
                  disabled={start.isPending}>
            {start.isPending && <Spinner />}
            Set up two-factor authentication
          </button>
        )}

        {secret && (
          <div className="space-y-4">
            <div>
              <p className="text-sm">Add this to your authenticator app:</p>
              <code className="mt-2 block break-all rounded bg-raised p-3 font-mono text-xs">
                {secret}
              </code>
              {uri && (
                <p className="hint mt-2 break-all">
                  Or use the setup URI: <span className="font-mono">{uri}</span>
                </p>
              )}
            </div>
            <Field label="Enter the current code to confirm" htmlFor="totp-confirm">
              <input id="totp-confirm" className="field max-w-[12rem] font-mono tracking-widest"
                     value={code} onChange={(e) => setCode(e.target.value)}
                     inputMode="numeric" autoComplete="one-time-code" />
            </Field>
            <button className="btn-primary" onClick={() => confirm.mutate()}
                    disabled={confirm.isPending || code.length < 6}>
              {confirm.isPending && <Spinner />}
              Confirm and enable
            </button>
          </div>
        )}
      </div>
    </Section>
  );
}

function SessionsSection() {
  const qc = useQueryClient();
  const sessions = useQuery({ queryKey: ["sessions"], queryFn: endpoints.sessions });

  const revoke = useMutation({
    mutationFn: (id: string) => endpoints.revokeSession(id),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["sessions"] }),
  });
  const revokeOthers = useMutation({
    mutationFn: endpoints.revokeOthers,
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["sessions"] }),
  });

  return (
    <Section title="Active sessions" description="Every device currently signed in.">
      <div className="space-y-2">
        {sessions.data?.map((s) => (
          <div key={s.id}
               className="flex items-center justify-between gap-4 rounded-md border
                          border-border px-3 py-2.5 text-sm">
            <div className="min-w-0">
              <p className="truncate">
                {s.user_agent ?? "Unknown device"}
                {s.current && (
                  <span className="ml-2 rounded bg-accent/15 px-1.5 py-0.5 text-[11px]
                                   font-medium text-accent">
                    this device
                  </span>
                )}
              </p>
              <p className="hint">
                {s.ip ?? "no address"} · active {relative(s.last_seen_at)} · expires{" "}
                {dateTime(s.expires_at)}
              </p>
            </div>
            {!s.current && (
              <button className="text-sm text-danger underline"
                      onClick={() => revoke.mutate(s.id)}>
                Revoke
              </button>
            )}
          </div>
        ))}
        {(sessions.data?.length ?? 0) > 1 && (
          <button className="btn-ghost mt-2" onClick={() => revokeOthers.mutate()}>
            Sign out all other sessions
          </button>
        )}
      </div>
    </Section>
  );
}

function AuditSection() {
  const audit = useQuery({ queryKey: ["audit"], queryFn: () => endpoints.audit(1) });

  return (
    <Section title="Recent activity" description="Authentication and administrative events.">
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-border text-left text-xs uppercase
                           tracking-wide text-muted">
              <th className="py-2 pr-4 font-medium">When</th>
              <th className="py-2 pr-4 font-medium">Action</th>
              <th className="py-2 pr-4 font-medium">Actor</th>
              <th className="py-2 font-medium">Address</th>
            </tr>
          </thead>
          <tbody>
            {audit.data?.items.slice(0, 15).map((e) => (
              <tr key={e.id} className="border-b border-border/60 last:border-0">
                <td className="whitespace-nowrap py-2 pr-4 text-muted">{dateTime(e.ts)}</td>
                <td className="py-2 pr-4 font-mono text-xs">{e.action}</td>
                <td className="py-2 pr-4">{e.actor ?? "—"}</td>
                <td className="py-2 font-mono text-xs text-muted">{e.ip || "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Section>
  );
}
