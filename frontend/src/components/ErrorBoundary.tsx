import { Component, type ErrorInfo, type ReactNode } from "react";

/**
 * Keeps one broken render from taking the whole app with it.
 *
 * Reported as a blank page that survived reloads: a non-URL saved into a
 * profile's verify URL made `new URL()` throw while rendering, React unmounted
 * everything, and because the value was stored the same thing happened on
 * every load. There was no way back — the page that could have fixed the
 * field was the page that would not render.
 *
 * The field is validated now and that particular call no longer throws, but
 * the property worth having is the general one: **bad data in one corner must
 * never cost the navigation.** Anything that gets you to another page is
 * enough to undo whatever caused it.
 */
export class ErrorBoundary extends Component<
  { children: ReactNode },
  { error: Error | null }
> {
  state: { error: Error | null } = { error: null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // Kept to the console rather than sent anywhere: this is a self-hosted
    // archiver and a crash report is not ours to collect.
    console.error("Cairn crashed while rendering", error, info.componentStack);
  }

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <div className="mx-auto max-w-2xl p-8">
        <h1 className="text-lg font-medium">Something in this page could not be shown</h1>
        <p className="hint mt-2">
          The archive itself is unaffected — this is the interface, not your captures. If it
          happens on every load, something saved is most likely the cause; the details below say
          where, and the console has the full trace.
        </p>
        <pre className="mt-3 overflow-x-auto rounded bg-raised p-3 font-mono text-xs">
          {this.state.error.message || String(this.state.error)}
        </pre>
        <div className="mt-4 flex gap-2">
          <button className="btn-ghost" onClick={() => this.setState({ error: null })}>
            Try again
          </button>
          {/* A hard navigation, not a router link: the router lives inside the
              tree that just failed, so anything softer can land straight back
              on the render that threw. */}
          <a className="btn-primary" href="/">
            Go to the dashboard
          </a>
        </div>
      </div>
    );
  }
}
