import { useState, type FormEvent } from "react";

import { Alert, Field, Logo, Spinner } from "../components/ui";
import { ApiError, endpoints } from "../lib/api";
import { useAuth } from "../lib/auth";

export default function Setup({ minLength }: { minLength: number }) {
  const { refresh } = useAuth();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [problems, setProblems] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);

  const mismatch = confirm.length > 0 && password !== confirm;

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    setProblems([]);
    if (password !== confirm) {
      setError("Passwords do not match.");
      return;
    }
    setBusy(true);
    try {
      await endpoints.setup(username, password);
      await refresh();
    } catch (err) {
      if (err instanceof ApiError) {
        setError(err.message);
        setProblems(err.problems);
      } else {
        setError("Could not reach the server.");
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex min-h-full items-center justify-center px-4 py-12">
      <div className="w-full max-w-md">
        <div className="mb-8 flex flex-col items-center text-center">
          <Logo className="h-11 w-11 text-accent" />
          <h1 className="mt-4 text-2xl font-semibold">Welcome to Cairn</h1>
          <p className="mt-1.5 text-sm text-muted">
            Create the single account that will control this archive.
          </p>
        </div>

        <form onSubmit={submit} className="card space-y-5 p-6">
          {error && (
            <Alert kind="error" title={error}>
              {problems.length > 0 && (
                <ul className="list-inside list-disc space-y-0.5">
                  {problems.map((p) => (
                    <li key={p}>{p}</li>
                  ))}
                </ul>
              )}
            </Alert>
          )}

          <Field label="Username" htmlFor="username">
            <input
              id="username"
              className="field"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              autoComplete="username"
              autoFocus
              required
              minLength={3}
              maxLength={64}
            />
          </Field>

          <Field
            label="Password"
            htmlFor="password"
            hint={`At least ${minLength} characters. A passphrase of a few words beats a short complex one.`}
          >
            <input
              id="password"
              type="password"
              className="field"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete="new-password"
              required
              minLength={minLength}
            />
          </Field>

          <Field label="Confirm password" htmlFor="confirm">
            <input
              id="confirm"
              type="password"
              className={`field ${mismatch ? "border-danger" : ""}`}
              value={confirm}
              onChange={(e) => setConfirm(e.target.value)}
              autoComplete="new-password"
              required
            />
          </Field>

          <button type="submit" className="btn-primary w-full" disabled={busy || mismatch}>
            {busy && <Spinner />}
            Create account
          </button>

          <p className="hint text-center">
            You can turn on two-factor authentication straight after, in Settings.
          </p>
        </form>
      </div>
    </div>
  );
}
