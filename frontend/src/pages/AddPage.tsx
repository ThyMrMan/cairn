import { useMutation, useQuery } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router-dom";

import { Alert, Spinner } from "../components/ui";
import { ApiError, endpoints } from "../lib/api";

/**
 * "Archive this page", arriving from the bookmarklet.
 *
 * The bookmarklet opens this page rather than posting anything itself, and
 * that is the whole security design. A `javascript:` bookmark runs on
 * *somebody else's* origin, so it cannot make an authenticated request to
 * Cairn — and giving it one would mean a token in a URL, which is a token in
 * browser history, in the referrer, and in every proxy log between here and
 * there. Opening a Cairn page instead lets the session cookie that is already
 * there do the work, and somebody who is not signed in gets the login screen,
 * which is the correct answer.
 *
 * Nothing submits automatically. A page anybody can link to must not archive
 * anything until the person looking at it says so.
 */
export default function AddPage() {
  const [params] = useSearchParams();
  const url = params.get("url") ?? "";
  const pageTitle = params.get("title") ?? "";

  const survey = useQuery({
    queryKey: ["add-survey", url],
    queryFn: () => endpoints.surveyUrls(url),
    enabled: Boolean(url),
  });
  const run = useMutation({
    mutationFn: () => endpoints.importUrls(url, { capture: true, crawl: false }),
  });

  const group = survey.data?.groups[0];
  const done = run.data;

  if (!url) {
    return (
      <div className="mx-auto max-w-lg p-6">
        <Alert kind="error" title="No address">
          This page archives one URL, passed in the address bar. It is what the bookmarklet in{" "}
          <Link to="/settings" className="underline">
            Settings
          </Link>{" "}
          opens.
        </Alert>
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-lg space-y-4 p-6">
      <h1 className="text-lg font-semibold">Archive this page</h1>

      <div className="card space-y-1 p-4">
        {pageTitle && <p className="text-sm font-medium">{pageTitle}</p>}
        <p className="break-all font-mono text-xs text-muted">{url}</p>
      </div>

      {survey.isLoading && <Spinner className="h-5 w-5 text-muted" />}
      {survey.error && <Alert kind="error">{(survey.error as ApiError).message}</Alert>}

      {group && !done && (
        <>
          <p className="text-sm">
            {group.is_new ? (
              <>
                This creates a new site for <strong>{group.key}</strong> and archives this one
                page. The rest of the site is not crawled.
              </>
            ) : (
              <>
                This page is added to <strong>{group.site_title}</strong>, which you already
                archive. Only this page is fetched.
              </>
            )}
          </p>
          <button
            className="btn-primary"
            onClick={() => run.mutate()}
            disabled={run.isPending}
          >
            {run.isPending && <Spinner />}
            Archive it
          </button>
        </>
      )}

      {run.error && <Alert kind="error">{(run.error as ApiError).message}</Alert>}

      {done && (
        <Alert kind="info" title="Queued">
          <p>
            {done.created.length ? "New site created" : "Added to an existing site"}, capture{" "}
            {done.jobs.length ? `queued as job #${done.jobs[0]}` : "not started"}.
          </p>
          <p className="mt-2 flex gap-3 text-sm">
            <Link
              to={`/sites/${done.created[0] ?? done.updated[0]}`}
              className="text-accent hover:underline"
            >
              Open the site
            </Link>
            <button className="hover:underline" onClick={() => window.close()}>
              Close this tab
            </button>
          </p>
        </Alert>
      )}
    </div>
  );
}
