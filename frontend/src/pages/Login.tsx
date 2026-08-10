import { useEffect, useRef, useState, type FormEvent } from "react";

import { Alert, Field, Logo, Spinner } from "../components/ui";
import { ApiError, endpoints } from "../lib/api";
import { useAuth } from "../lib/auth";

export default function Login() {
  const { refresh } = useAuth();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [totp, setTotp] = useState("");
  const [needsTotp, setNeedsTotp] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const totpRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (needsTotp) totpRef.current?.focus();
  }, [needsTotp]);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      await endpoints.login(username, password, totp);
      await refresh();
    } catch (err) {
      if (err instanceof ApiError) {
        if (err.code === "totp_required") {
          // Password was accepted — move to the second step rather than
          // showing this as a failure.
          setNeedsTotp(true);
          setError(null);
        } else {
          setError(err.message);
          setTotp("");
        }
      } else {
        setError("Could not reach the server.");
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex min-h-full items-center justify-center px-4 py-12">
      <div className="w-full max-w-sm">
        <div className="mb-8 flex flex-col items-center text-center">
          <Logo className="h-10 w-10 text-accent" />
          <h1 className="mt-4 text-xl font-semibold">Sign in to Cairn</h1>
        </div>

        <form onSubmit={submit} className="card space-y-5 p-6">
          {error && <Alert kind="error">{error}</Alert>}

          <Field label="Username" htmlFor="username">
            <input
              id="username"
              className="field"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              autoComplete="username"
              autoFocus
              required
              disabled={needsTotp}
            />
          </Field>

          <Field label="Password" htmlFor="password">
            <input
              id="password"
              type="password"
              className="field"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete="current-password"
              required
              disabled={needsTotp}
            />
          </Field>

          {needsTotp && (
            <Field
              label="Authentication code"
              htmlFor="totp"
              hint="Six digits from your authenticator app, or one of your recovery codes."
            >
              <input
                id="totp"
                ref={totpRef}
                className="field font-mono tracking-widest"
                value={totp}
                onChange={(e) => setTotp(e.target.value)}
                autoComplete="one-time-code"
                inputMode="text"
                required
              />
            </Field>
          )}

          <button type="submit" className="btn-primary w-full" disabled={busy}>
            {busy && <Spinner />}
            {needsTotp ? "Verify" : "Sign in"}
          </button>

          {needsTotp && (
            <button
              type="button"
              className="hint w-full text-center underline"
              onClick={() => {
                setNeedsTotp(false);
                setTotp("");
                setError(null);
              }}
            >
              Start over
            </button>
          )}
        </form>

        {/* Seeing this instead of the setup page means an account already
            exists. That is usually a reused /config volume, and without a
            hint the only visible symptom is "the setup screen is missing". */}
        <details className="mt-6 text-center">
          <summary className="hint cursor-pointer select-none">
            Expecting a first-time setup screen?
          </summary>
          <div className="hint mt-3 space-y-2 text-left">
            <p>
              This instance already has an account, so setup is closed. That
              usually means the config volume carries over from an earlier run.
            </p>
            <p>Check from the host:</p>
            <code className="block rounded bg-raised p-2 font-mono text-[11px]">
              docker exec cairn cairn users
            </code>
            <p>Forgotten the password, or locked out:</p>
            <code className="block rounded bg-raised p-2 font-mono text-[11px]">
              docker exec -it cairn cairn reset-password &lt;username&gt;
            </code>
          </div>
        </details>
      </div>
    </div>
  );
}
