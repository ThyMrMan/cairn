import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";

import { MediaPolicyFields } from "../components/Media";
import { Matches, MatchesFootnote, usePatternMatches } from "../components/PatternMatches";
import { Alert, Field, Spinner } from "../components/ui";
import { ApiError, endpoints } from "../lib/api";
import { useAuth } from "../lib/auth";
import { bytes, dateTime, relative } from "../lib/format";
import { useVersion } from "../lib/version";

export default function Settings() {
  return (
    <div className="space-y-8">
      <header>
        <h1 className="text-2xl font-semibold">Settings</h1>
        <p className="mt-1 text-sm text-muted">This instance, your account, and security.</p>
      </header>
      <AboutSection />
      <ScheduleSection />
      <NotificationsSection />
      <IntegritySection />
      <ThumbnailSection />
      <SkipPatternsSection />
      <MediaDefaultsSection />
      <StorageSection />
      <MirrorSection />
      <BookmarkletSection />
      <UrlImportSection />
      <ImportSection />
      <MetricsSection />
      <TagsSection />
      <TrashSection />
      <PasswordSection />
      <TotpSection />
      <SessionsSection />
      <AuditSection />
    </div>
  );
}

/**
 * Checking a copy of the archive.
 *
 * Not a sync — making the copy is rsync's job, or restic's, and both are
 * years ahead of anything worth writing here. What this instance has and they
 * do not is the checksum taken when each file was written, so it can answer
 * the question they cannot: is the copy *complete*, and are its bytes still
 * the bytes.
 *
 * The listing runs first because it is instant and catches the common failure
 * — a sync that skipped a directory and reported success. Re-checksumming
 * reads every byte of the copy and is therefore a job.
 */
function MirrorSection() {
  const [path, setPath] = useState("");
  const [queued, setQueued] = useState<string | null>(null);

  const survey = useMutation({ mutationFn: () => endpoints.surveyMirror(path.trim()) });
  const verify = useMutation({
    mutationFn: () => endpoints.verifyMirror(path.trim()),
    onSuccess: (result) =>
      setQueued(`Queued as job #${result.job_id}. Nothing is written to the copy.`),
  });

  const found = survey.data;

  return (
    <Section
      title="Check a backup copy"
      description="Point this at a mounted copy of /data made with rsync, restic or anything else. It reports which captures the copy is missing, and can re-checksum every file against what was recorded when it was written."
    >
      <div className="flex flex-wrap gap-2">
        <input
          className="input min-w-0 flex-1 font-mono text-sm"
          placeholder="/backup"
          value={path}
          onChange={(e) => {
            setPath(e.target.value);
            survey.reset();
            setQueued(null);
          }}
          aria-label="Path to the copy"
        />
        <button
          className="btn-ghost"
          disabled={!path.trim() || survey.isPending}
          onClick={() => survey.mutate()}
        >
          {survey.isPending && <Spinner />}
          Look
        </button>
      </div>
      <p className="hint mt-2">
        The path as <em>this container</em> sees it — mount the backup read-only, for example{" "}
        <code>-v /mnt/backup/cairn:/backup:ro</code>, then type <code>/backup</code>.
      </p>

      {survey.error && <Alert kind="error">{(survey.error as ApiError).message}</Alert>}

      {found && (
        <div className="mt-4 space-y-2 text-sm">
          <p className={found.complete ? "text-ok" : "text-warn"}>
            {found.present.toLocaleString()} of {found.captures.toLocaleString()} capture(s)
            are in the copy
            {found.complete ? "." : `, ${found.missing.toLocaleString()} missing.`}
          </p>
          {found.sites.length > 0 && (
            <ul className="space-y-1 text-xs text-muted">
              {found.sites.slice(0, 10).map((site) => (
                <li key={site.site_id}>
                  {site.title} — {site.present}/{site.captures} captures
                </li>
              ))}
            </ul>
          )}
          {found.unknown_dirs.length > 0 && (
            <p className="hint">
              {found.unknown_dirs.length} site director(ies) in the copy are not sites here.
              Usually sites you have since deleted — or the copy belongs to a different
              instance.
            </p>
          )}
          <button className="btn-ghost" onClick={() => verify.mutate()} disabled={verify.isPending}>
            {verify.isPending && <Spinner />}
            Re-checksum the copy
          </button>
          {queued && <p className="text-xs text-muted">{queued}</p>}
          {verify.error && (
            <p className="text-xs text-danger">{(verify.error as ApiError).message}</p>
          )}
        </div>
      )}
    </Section>
  );
}

/**
 * The bookmarklet.
 *
 * A `javascript:` bookmark, not a browser extension: no store, no review, no
 * second codebase to keep in step, and it works in every browser that has a
 * bookmarks bar. It carries no credential of any kind — it opens a Cairn page
 * and lets the session cookie already in that browser do the work.
 *
 * The address is read from the window rather than configured, because the
 * window is by definition an address that reaches this instance. An install
 * behind a reverse proxy therefore gets the proxy's address, which is the one
 * that will work from a bookmarks bar.
 */
function BookmarkletSection() {
  const origin = window.location.origin;
  // Assembled rather than written as a template literal so the `javascript:`
  // URL is one line with no newlines — several browsers refuse a bookmark
  // whose URL contains them.
  const code =
    "javascript:(function(){window.open('" +
    origin +
    "/add?url='+encodeURIComponent(location.href)+'&title='+encodeURIComponent(document.title)," +
    "'cairn','width=560,height=520')})()";

  return (
    <Section
      title="Archive this page"
      description="A bookmarklet. Drag it to your bookmarks bar, then press it on any page to archive that page — only that page, not the whole site."
    >
      <p className="text-sm">
        <a
          // Set on the element rather than passed as a prop: React warns
          // about a `javascript:` href and will one day refuse it, and an
          // anchor with a real href is the only thing a browser will let you
          // drag into a bookmarks bar. Nothing here executes on this page —
          // the click handler stops that — it exists to be dragged.
          ref={(el) => el?.setAttribute("href", code)}
          className="btn-ghost inline-block"
          onClick={(e) => e.preventDefault()}
          title="Drag me to the bookmarks bar"
        >
          Archive to Cairn
        </a>
      </p>
      <p className="hint mt-2">
        Dragging is the only way to install it — clicking it here does nothing. It opens a small
        Cairn window that asks before archiving anything, and it carries no password or token: if
        you are not signed in, you get the sign-in page.
      </p>
      <details className="mt-3">
        <summary className="cursor-pointer text-xs text-muted">
          If your browser will not let you drag it
        </summary>
        <p className="hint mt-2">
          Make a new bookmark by hand and paste this as its address:
        </p>
        <textarea
          readOnly
          className="field mt-2 h-24 w-full font-mono text-[11px]"
          value={code}
          onFocus={(e) => e.currentTarget.select()}
        />
        <p className="hint mt-2">
          It points at <code>{origin}</code> — the address this page is open at. If you reach
          Cairn by a different name from another machine, edit that part.
        </p>
      </details>
    </Section>
  );
}

/**
 * A pasted list of URLs.
 *
 * The survey always runs first, because the two things worth knowing before
 * pressing the button — how many *sites* this becomes, and which of them
 * already exist — are not visible in the list itself. Crawling is off unless
 * asked for: fifty bookmarks are fifty pages, and turning them into fifty full
 * crawls of fifty strangers' sites is how an IP address gets blocked.
 */
function UrlImportSection() {
  const [text, setText] = useState("");
  const [crawl, setCrawl] = useState(false);
  const [done, setDone] = useState<string | null>(null);

  const survey = useMutation({ mutationFn: () => endpoints.surveyUrls(text) });
  const run = useMutation({
    mutationFn: () => endpoints.importUrls(text, { capture: true, crawl }),
    onSuccess: (result) =>
      setDone(
        `${result.created.length} site(s) created, ${result.updated.length} reused, ` +
          `${result.jobs.length} capture(s) queued for ${result.urls} URL(s).`,
      ),
  });

  const found = survey.data;

  return (
    <Section
      title="Import a list of URLs"
      description="Paste anything containing links — a bookmark export, a markdown list, a spreadsheet column — and every http(s) address in it is grouped by domain into sites."
    >
      <textarea
        className="field h-32 w-full font-mono text-xs"
        placeholder={"https://example.com/a-post\nhttps://another.example/something-else"}
        value={text}
        onChange={(e) => {
          setText(e.target.value);
          survey.reset();
          setDone(null);
        }}
        aria-label="URLs to import"
      />
      <div className="mt-2 flex flex-wrap items-center gap-3">
        <button
          className="btn-ghost"
          disabled={!text.trim() || survey.isPending}
          onClick={() => survey.mutate()}
        >
          {survey.isPending && <Spinner />}
          Look
        </button>
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" checked={crawl} onChange={(e) => setCrawl(e.target.checked)} />
          Crawl each site as well
        </label>
      </div>
      <p className="hint mt-2">
        Without that box ticked, exactly the pages you listed are archived and nothing else.
        With it, each site is crawled in full — which for a long list is a great deal of
        somebody else&rsquo;s bandwidth and yours.
      </p>

      {survey.error && <Alert kind="error">{(survey.error as ApiError).message}</Alert>}

      {found && (
        <div className="mt-4 space-y-2 text-sm">
          <p>
            {found.found.toLocaleString()} URL(s) → {found.new_sites} new site(s),{" "}
            {found.existing_sites} already here.
          </p>
          <ul className="space-y-1 text-xs text-muted">
            {found.groups.slice(0, 12).map((group) => (
              <li key={group.key}>
                <span className="font-mono">{group.key}</span> — {group.url_count} page(s)
                {group.is_new ? " · new site" : ` · into “${group.site_title}”`}
              </li>
            ))}
          </ul>
          {found.skipped_count > 0 && (
            <p className="text-xs text-warn">{found.skipped_count} line(s) skipped.</p>
          )}
          <button
            className="btn-primary"
            disabled={!found.found || run.isPending}
            onClick={() => run.mutate()}
          >
            {run.isPending && <Spinner />}
            Import {found.groups.length} site(s)
          </button>
          {done && <p className="text-xs text-ok">{done}</p>}
          {run.error && <p className="text-xs text-danger">{(run.error as ApiError).message}</p>}
        </div>
      )}
    </Section>
  );
}

/**
 * Importing an existing ArchiveBox archive.
 *
 * The survey runs first and always: an ArchiveBox is full of pages archived
 * with extractors that write no WARC, and how many of them there are is the
 * only number worth seeing before starting.
 */
function ImportSection() {
  const [path, setPath] = useState("");
  const [queued, setQueued] = useState<string | null>(null);

  const survey = useMutation({ mutationFn: () => endpoints.surveyArchiveBox(path.trim()) });
  const run = useMutation({
    mutationFn: () => endpoints.importArchiveBox(path.trim()),
    onSuccess: (result) =>
      setQueued(`Queued as job #${result.job_id}. Your ArchiveBox is copied, never modified.`),
  });

  const found = survey.data;

  return (
    <Section
      title="Import from ArchiveBox"
      description="Reads an ArchiveBox data directory and brings each domain across as a site. The source archive is copied, never moved or written to."
    >
      <div className="flex flex-wrap gap-2">
        <input
          className="input min-w-0 flex-1 font-mono text-sm"
          placeholder="/import"
          value={path}
          onChange={(e) => {
            setPath(e.target.value);
            survey.reset();
          }}
          aria-label="ArchiveBox data directory"
        />
        <button
          className="btn-ghost"
          disabled={!path.trim() || survey.isPending}
          onClick={() => survey.mutate()}
        >
          {survey.isPending && <Spinner />}
          Look
        </button>
      </div>
      <p className="hint mt-2">
        The path as <em>this container</em> sees it — mount your ArchiveBox data directory in,
        for example <code>-v /mnt/user/archivebox:/import:ro</code>, then type{" "}
        <code>/import</code>.
      </p>

      {survey.error && (
        <Alert kind="error">{(survey.error as ApiError).message}</Alert>
      )}

      {found && (
        <div className="mt-4 space-y-2 text-sm">
          <p>
            {found.snapshots.toLocaleString()} snapshot(s), {found.with_warcs.toLocaleString()}{" "}
            with a WARC ({bytes(found.warc_bytes)}), layout {found.version}.
          </p>
          {found.problems.map((problem) => (
            <p key={problem} className="text-warn">
              {problem}
            </p>
          ))}
          <ul className="text-xs text-muted">
            {Object.entries(found.hosts)
              .slice(0, 10)
              .map(([host, count]) => (
                <li key={host}>
                  {host} — {count} snapshot(s)
                </li>
              ))}
          </ul>
          <button
            className="btn-ghost"
            disabled={!found.with_warcs || run.isPending}
            onClick={() => run.mutate()}
          >
            {run.isPending && <Spinner />}
            Import {Object.keys(found.hosts).length} site(s)
          </button>
          {queued && <p className="text-xs text-muted">{queued}</p>}
          {run.error && <p className="text-xs text-danger">{(run.error as ApiError).message}</p>}
        </div>
      )}
    </Section>
  );
}

/** Prometheus. Off by default; see services/metrics.py for what it never carries. */
function MetricsSection() {
  const client = useQueryClient();
  const config = useQuery({ queryKey: ["metrics-settings"], queryFn: endpoints.metricsSettings });
  const save = useMutation({
    mutationFn: (body: { enabled?: boolean; token?: string }) =>
      endpoints.putMetricsSettings(body),
    onSuccess: (data) => client.setQueryData(["metrics-settings"], data),
  });
  const [draft, setDraft] = useState("");

  const enabled = Boolean(config.data?.enabled);

  return (
    <Section
      title="Prometheus metrics"
      description="An /api/metrics endpoint for a scraper. Off by default. It carries counts only — no site name, URL, host, folder or tag appears in it, because a scraper cannot log in."
    >
      <label className="flex items-center gap-2 text-sm">
        <input
          type="checkbox"
          checked={enabled}
          onChange={(e) => save.mutate({ enabled: e.target.checked })}
        />
        Expose <code>/api/metrics</code>
      </label>
      {enabled && (
        <div className="mt-3 space-y-2">
          <Field
            label="Bearer token"
            hint={
              config.data?.token_set
                ? "A token is set. Type a new one to replace it, or clear the box and save to remove it."
                : "Optional. Prometheus sends this with bearer_token in its scrape config."
            }
          >
            <input
              className="input font-mono text-sm"
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onBlur={() => save.mutate({ token: draft })}
              placeholder={config.data?.token_set ? "•••••••• (set)" : "none"}
            />
          </Field>
          {save.error && (
            <p className="text-xs text-danger">{(save.error as ApiError).message}</p>
          )}
        </div>
      )}
    </Section>
  );
}

function ThumbnailSection() {
  const client = useQueryClient();
  const config = useQuery({ queryKey: ["thumbnail-settings"], queryFn: endpoints.thumbnailSettings });
  const save = useMutation({
    mutationFn: (enabled: boolean) => endpoints.putThumbnailSettings({ enabled }),
    onSuccess: (data) => client.setQueryData(["thumbnail-settings"], data),
  });
  const [queued, setQueued] = useState<string | null>(null);
  const run = useMutation({
    mutationFn: (force: boolean) => endpoints.rebuildThumbnails({ force }),
    onSuccess: (result, force) => {
      setQueued(
        `Queued as job #${result.job_id}. It loads one archived page per site${
          force ? ", including the ones that already have a picture" : ""
        }, so watch it on the Jobs page.`,
      );
      void client.invalidateQueries({ queryKey: ["jobs"] });
    },
  });

  return (
    <Section
      title="Site thumbnails"
      description="A picture of each site's archived front page, taken through replay rather than off the live web — so it shows what is in the archive, not what the domain serves today."
    >
      <label className="flex items-center gap-2 text-sm">
        <input
          type="checkbox"
          checked={Boolean(config.data?.enabled)}
          onChange={(e) => save.mutate(e.target.checked)}
        />
        Take one after each capture
      </label>
      <p className="mt-2 text-xs text-muted">
        Only when that capture changed the page it would show, so an incremental capture of one
        new post does not start a browser. Needs replay to be running.
      </p>
      <div className="mt-4 flex flex-wrap gap-2">
        <button
          className="btn-ghost text-xs"
          onClick={() => run.mutate(false)}
          disabled={run.isPending}
        >
          {run.isPending && <Spinner />}
          Take the missing ones
        </button>
        <button
          className="btn-ghost text-xs"
          onClick={() => run.mutate(true)}
          disabled={run.isPending}
        >
          Retake every one
        </button>
      </div>
      {queued && <p className="mt-2 text-xs text-muted">{queued}</p>}
      {run.error && <p className="mt-2 text-xs text-danger">{(run.error as ApiError).message}</p>}
      {save.error && <p className="mt-2 text-xs text-danger">{(save.error as ApiError).message}</p>}
    </Section>
  );
}

/**
 * Reject patterns that apply to every site.
 *
 * Not a default new sites inherit — it is merged into each scope as the scope
 * is resolved, so this list stays the only copy. Adding a pattern changes what
 * every site's next capture fetches without visiting any of them, and removing
 * one removes it everywhere rather than leaving it behind in whichever sites
 * happened to exist when it was added.
 *
 * A site can turn an individual entry off for itself from its own domain
 * picker. That escape hatch is why a list like this is safe to have at all: a
 * rule that is right for the web in general and wrong for one blog would
 * otherwise mean deleting it for everybody.
 */
function SkipPatternsSection() {
  const client = useQueryClient();
  const list = useQuery({ queryKey: ["skip-patterns"], queryFn: endpoints.skipPatterns });
  const save = useMutation({
    mutationFn: (patterns: string[]) => endpoints.putSkipPatterns(patterns),
    onSuccess: (data) => {
      client.setQueryData(["skip-patterns"], data);
      // Every site's scope panel and pre-capture summary counts these.
      void client.invalidateQueries({ queryKey: ["scope"] });
      void client.invalidateQueries({ queryKey: ["site"] });
    },
  });
  const [value, setValue] = useState("");
  const patterns = list.data?.patterns ?? [];
  const check = usePatternMatches(patterns);

  function add(event: FormEvent) {
    event.preventDefault();
    const trimmed = value.trim();
    if (!trimmed) return;
    // The API de-duplicates too, but doing it here keeps the field from
    // clearing as though something happened when nothing did.
    if (patterns.includes(trimmed)) return;
    save.mutate([...patterns, trimmed]);
    setValue("");
  }

  return (
    <Section
      title="Skip these URLs everywhere"
      description="Regular expressions matched against every URL, on every site. A pattern here applies to captures that have not run yet and to what replay serves — it is the same rule as a site's own skip list, written once."
    >
      {list.isPending && <Spinner className="h-4 w-4 text-muted" />}
      <ul className="space-y-1">
        {patterns.map((pattern) => (
          <li key={pattern} className="flex items-center gap-3">
            <code className="min-w-0 flex-1 truncate text-xs">{pattern}</code>
            <Matches check={check.data} pattern={pattern} />
            <button
              className="shrink-0 text-xs text-muted hover:text-danger"
              disabled={save.isPending}
              onClick={() => save.mutate(patterns.filter((p) => p !== pattern))}
            >
              remove
            </button>
          </li>
        ))}
        {!list.isPending && patterns.length === 0 && (
          <li className="text-xs text-muted">
            Nothing yet. Tracking parameters are the usual candidates —{" "}
            <code>[?&amp;]utm_[a-z]+=</code> or <code>[?&amp;]fbclid=</code> — because they change
            the URL without changing the page, so each one archives the same content again.
          </li>
        )}
      </ul>
      <form className="mt-3 flex gap-2" onSubmit={add}>
        <input
          className="field text-xs"
          placeholder="[?&amp;]utm_[a-z]+="
          value={value}
          onChange={(event) => setValue(event.target.value)}
        />
        <button className="btn-ghost text-xs" disabled={save.isPending}>
          {save.isPending && <Spinner />}
          Add
        </button>
      </form>
      {save.error && <p className="mt-2 text-xs text-danger">{(save.error as ApiError).message}</p>}
      <MatchesFootnote check={check.data} />
      <p className="mt-3 text-xs text-muted">
        Already-captured pages are not deleted. A pattern added now stops the next capture fetching
        those URLs and hides the ones already archived from replay; removing it brings them back.
        Any site can turn an individual pattern off for itself in its own <em>Scope</em> panel.
      </p>
    </Section>
  );
}

/**
 * The embedded-media default every site inherits.
 *
 * A site that has been given its own answer keeps it, so switching this on
 * does not start downloading video across an existing archive — it decides
 * what a site gets when nobody has said otherwise, which in practice means
 * every site added afterwards.
 */
function MediaDefaultsSection() {
  const client = useQueryClient();
  const config = useQuery({ queryKey: ["media-defaults"], queryFn: endpoints.mediaDefaults });
  const save = useMutation({
    mutationFn: (body: Record<string, unknown>) => endpoints.putMediaDefaults(body),
    onSuccess: (data) => {
      client.setQueryData(["media-defaults"], data);
      // A site page showing an inherited value is now showing a stale one.
      void client.invalidateQueries({ queryKey: ["media"] });
    },
  });

  return (
    <Section
      title="Embedded video and audio"
      description="No crawler captures a video stream, so an archived post with a YouTube embed keeps a dead rectangle. This is the default for sites that have not been given their own answer; each site can still override it on its own page."
    >
      {config.data && !config.data.available && (
        <Alert kind="warn">
          {config.data.unavailable_reason || "yt-dlp is not available."}
        </Alert>
      )}
      {config.data && (
        <MediaPolicyFields
          policy={config.data.policy}
          hosts={config.data.hosts}
          pending={save.isPending}
          onCommit={(body) => save.mutate(body)}
          scope="instance"
        />
      )}
      <p className="mt-3 text-xs text-muted">
        Off by default, and worth leaving off unless you mean it: a blog&rsquo;s text and images
        are megabytes, its embedded video is gigabytes, and this is the one setting here that can
        fill a disk on a schedule set months ago.
      </p>
      {Object.keys(config.data?.override ?? {}).length > 0 && (
        <button
          className="btn-ghost mt-3 text-xs"
          disabled={save.isPending}
          onClick={() => save.mutate({})}
        >
          Back to the built-in defaults
        </button>
      )}
      {save.error && <p className="mt-2 text-xs text-danger">{(save.error as ApiError).message}</p>}
    </Section>
  );
}

/**
 * Archive health.
 *
 * The number to watch is the oldest capture nothing has checked. A pass that
 * has quietly stopped reaching half the archive still reports success on the
 * half it reaches, and only that figure makes the gap visible.
 */
function IntegritySection() {
  const client = useQueryClient();
  const health = useQuery({ queryKey: ["integrity"], queryFn: endpoints.integrity });
  const [queued, setQueued] = useState<string | null>(null);

  const run = useMutation({
    mutationFn: (deep: boolean) => endpoints.verifyArchive({ deep }),
    onSuccess: (result, deep) => {
      setQueued(
        `Queued as job #${result.job_id}. It reads every archived byte${
          deep ? ", twice — once to checksum and once to parse" : ""
        }, so watch it on the Jobs page.`,
      );
      void client.invalidateQueries({ queryKey: ["jobs"] });
    },
  });

  const data = health.data;
  const last = data?.last_run;
  const findings = last?.findings ?? [];

  return (
    <Section
      title="Archive health"
      description="Re-reads every archived file and compares it to the checksum taken when it was written. It never repairs anything: a WARC cannot be corrected, only restored or captured again."
    >
      {health.isLoading && <Spinner className="h-4 w-4 text-muted" />}
      {data && (
        <>
          <dl className="grid gap-x-8 gap-y-2 text-sm sm:grid-cols-2">
            <div className="flex justify-between gap-4">
              <dt className="text-muted">Captures verified</dt>
              <dd className="tabular-nums">
                {data.verified.toLocaleString()} of {data.captures.toLocaleString()}
              </dd>
            </div>
            <div className="flex justify-between gap-4">
              <dt className="text-muted">Last run</dt>
              <dd>{last?.finished_at ? relative(last.finished_at) : "never"}</dd>
            </div>
            {last && (
              <>
                <div className="flex justify-between gap-4">
                  <dt className="text-muted">Read</dt>
                  <dd className="tabular-nums">
                    {bytes(last.bytes_read)} across {last.files.toLocaleString()} file(s)
                  </dd>
                </div>
                <div className="flex justify-between gap-4">
                  <dt className="text-muted">Findings</dt>
                  <dd className={findings.length ? "text-danger tabular-nums" : "tabular-nums"}>
                    {findings.length}
                  </dd>
                </div>
              </>
            )}
          </dl>

          {data.oldest_unverified && (
            <p className="mt-3 text-sm text-muted">
              Oldest capture nothing has checked:{" "}
              <a className="text-accent hover:underline" href={`/sites/${data.oldest_unverified.site_id}`}>
                {data.oldest_unverified.site_title}
              </a>{" "}
              <span className="font-mono text-xs">{data.oldest_unverified.dir_name}</span>, from{" "}
              {dateTime(data.oldest_unverified.started_at)}.
            </p>
          )}

          {findings.length > 0 && (
            <Alert kind="error" title={`${findings.length} finding(s)`}>
              <ul className="space-y-2">
                {findings.slice(0, 10).map((finding, i) => (
                  <li key={i}>
                    <span className="font-mono text-xs">{finding.kind}</span>{" "}
                    <strong>{finding.site_title}</strong>
                    {finding.file ? ` — ${finding.file}` : ""}
                    <br />
                    <span className="text-muted">{finding.detail}</span>
                  </li>
                ))}
              </ul>
              {findings.length > 10 && <p className="mt-2">…and {findings.length - 10} more.</p>}
            </Alert>
          )}

          <div className="mt-4 flex flex-wrap gap-2">
            <button
              className="btn-ghost text-xs"
              onClick={() => run.mutate(false)}
              disabled={run.isPending}
            >
              {run.isPending && <Spinner />}
              Verify now
            </button>
            <button
              className="btn-ghost text-xs"
              onClick={() => run.mutate(true)}
              disabled={run.isPending}
            >
              Verify and parse every WARC
            </button>
          </div>
          {queued && <p className="mt-2 text-xs text-muted">{queued}</p>}
          {run.error && (
            <p className="mt-2 text-xs text-danger">{(run.error as ApiError).message}</p>
          )}
        </>
      )}
    </Section>
  );
}

function StorageSection() {
  const client = useQueryClient();
  const storage = useQuery({ queryKey: ["storage"], queryFn: endpoints.storage });
  const [result, setResult] = useState<string | null>(null);
  const data = storage.data;

  return (
    <Section
      title="Storage"
      description="Per-site totals are measured at the end of each capture, not by walking the
                  array on every page load. Free space and trash are read live."
    >
      {data && (
        <>
          <dl className="grid max-w-lg grid-cols-[10rem_1fr] gap-y-2 text-sm">
            <dt className="text-muted">Archives</dt>
            <dd className="tabular-nums">
              {bytes(data.archives_bytes)} across {data.sites} site(s)
            </dd>
            <dt className="text-muted">Trash</dt>
            <dd className="tabular-nums">
              {bytes(data.trash_bytes)} across {data.trash_sites} site(s)
            </dd>
            <dt className="text-muted">Free on disk</dt>
            <dd className="tabular-nums">
              {bytes(data.free_bytes)} of {bytes(data.total_bytes)}
            </dd>
          </dl>

          {data.folders.length > 0 && (
            <table className="mt-4 w-full text-sm">
              <thead className="text-left text-xs uppercase text-muted">
                <tr>
                  <th className="py-1 font-medium">Folder</th>
                  <th className="py-1 text-right font-medium">Sites</th>
                  <th className="py-1 text-right font-medium">Size</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {data.folders.map((folder) => (
                  <tr key={folder.id}>
                    <td className="py-1.5 font-mono text-xs">{folder.path}</td>
                    <td className="py-1.5 text-right tabular-nums">{folder.total_site_count}</td>
                    <td className="py-1.5 text-right tabular-nums">
                      {bytes(folder.total_size_bytes)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </>
      )}

      <div className="mt-4 flex flex-wrap gap-2">
        <button
          className="btn-ghost text-xs"
          onClick={async () => {
            const out = await endpoints.rebuildSymlinks();
            setResult(`Tag tree rebuilt: ${out.linked} link(s), ${out.removed} removed.`);
            await client.invalidateQueries({ queryKey: ["storage"] });
          }}
        >
          Rebuild the tag tree
        </button>
        <button
          className="btn-ghost text-xs"
          onClick={async () => {
            const out = await endpoints.rebuildCollections();
            setResult(`Replay collections re-pointed: ${out.linked} linked, ${out.removed} removed.`);
          }}
        >
          Re-point replay collections
        </button>
        <button
          className="btn-ghost text-xs"
          title="Re-decide partial captures from what each one recorded. It can only clear a partial, never create one."
          onClick={async () => {
            const out = await endpoints.recomputeStatus();
            const kept = out.captures.filter((c) => !c.changed);
            setResult(
              out.changed === 0
                ? `Examined ${out.examined} partial capture(s); none needed changing.`
                : `${out.changed} of ${out.examined} partial capture(s) are now ok` +
                  // Name what it refused. "3 changed" without saying which is
                  // the same unexplained answer this action exists to correct.
                  (kept.length ? `; ${kept.length} left as they were.` : "."),
            );
            await client.invalidateQueries({ queryKey: ["sites"] });
          }}
        >
          Recheck partial captures
        </button>
      </div>
      {result && (
        <p className="mt-2 text-xs text-muted">{result}</p>
      )}
    </Section>
  );
}

function TagsSection() {
  const client = useQueryClient();
  const tags = useQuery({ queryKey: ["tags"], queryFn: endpoints.tags });
  const [error, setError] = useState<string | null>(null);

  const refresh = async () => {
    await client.invalidateQueries({ queryKey: ["tags"] });
    await client.invalidateQueries({ queryKey: ["sites"] });
  };

  const guard = async (fn: () => Promise<unknown>) => {
    setError(null);
    try {
      await fn();
      await refresh();
    } catch (err) {
      setError((err as ApiError).message);
    }
  };

  return (
    <Section
      title="Tags"
      description="Each tag is also a directory under /data/by-tag holding a symlink per site,
                  so the same grouping is browsable over SMB. Renaming a tag moves that
                  directory."
    >
      {error && <Alert kind="error">{error}</Alert>}
      {tags.data?.length ? (
        <ul className="divide-y divide-border">
          {tags.data.map((tag) => (
            <li key={tag.id} className="flex flex-wrap items-center gap-3 py-2 text-sm">
              <input
                type="color"
                className="h-6 w-8 shrink-0 rounded border border-border bg-transparent"
                value={tag.color ?? "#888888"}
                aria-label={`Colour for ${tag.name}`}
                onChange={(event) =>
                  void guard(() => endpoints.updateTag(tag.id, { color: event.target.value }))
                }
              />
              <span className="flex-1 font-medium">{tag.name}</span>
              <span className="font-mono text-xs text-muted">by-tag/{tag.slug}</span>
              <span className="w-16 text-right text-xs text-muted">{tag.site_count} site(s)</span>
              <button
                className="btn-ghost text-xs text-danger"
                onClick={() => void guard(() => endpoints.deleteTag(tag.id))}
              >
                Delete
              </button>
            </li>
          ))}
        </ul>
      ) : (
        <p className="text-sm text-muted">
          No tags yet. Add one when creating a site, or from the bulk actions on the Sites page.
        </p>
      )}
    </Section>
  );
}

function TrashSection() {
  const client = useQueryClient();
  const trash = useQuery({ queryKey: ["trash"], queryFn: endpoints.trash });
  const [error, setError] = useState<string | null>(null);
  const [confirmEmpty, setConfirmEmpty] = useState(false);

  const guard = async (fn: () => Promise<unknown>) => {
    setError(null);
    try {
      await fn();
      await client.invalidateQueries({ queryKey: ["trash"] });
      await client.invalidateQueries({ queryKey: ["sites"] });
      await client.invalidateQueries({ queryKey: ["storage"] });
    } catch (err) {
      setError((err as ApiError).message);
    }
  };

  return (
    <Section
      title="Trash"
      description="Deleted sites keep their archive until they are purged. The sweep runs when
                  the container starts, so the retention window is a floor on how long
                  something is kept, not a promise about when it goes."
    >
      {error && <Alert kind="error">{error}</Alert>}
      {trash.data?.length ? (
        <>
          <ul className="divide-y divide-border">
            {trash.data.map((entry) => (
              <li key={entry.id} className="flex flex-wrap items-center gap-3 py-2 text-sm">
                <div className="min-w-0 flex-1">
                  <p className="truncate font-medium">{entry.title}</p>
                  <p className="truncate text-xs text-muted">
                    {entry.folder_path} · deleted {relative(entry.deleted_at)}
                    {entry.on_disk
                      ? ` · purged in ${entry.purge_after_days} day(s)`
                      : " · the archive is already gone"}
                  </p>
                </div>
                <span className="shrink-0 text-xs tabular-nums text-muted">
                  {bytes(entry.size_bytes)}
                </span>
                <button
                  className="btn-ghost text-xs"
                  onClick={() => void guard(() => endpoints.restoreSite(entry.id))}
                >
                  Restore
                </button>
                <button
                  className="btn-ghost text-xs text-danger"
                  onClick={() => void guard(() => endpoints.purgeSite(entry.id))}
                >
                  Delete for good
                </button>
              </li>
            ))}
          </ul>
          <div className="mt-4 flex flex-wrap gap-2">
            {confirmEmpty ? (
              <>
                <button
                  className="btn-danger text-xs"
                  onClick={() =>
                    void guard(async () => {
                      await endpoints.emptyTrash();
                      setConfirmEmpty(false);
                    })
                  }
                >
                  Yes, delete {trash.data.length} archive(s) permanently
                </button>
                <button className="btn-ghost text-xs" onClick={() => setConfirmEmpty(false)}>
                  Cancel
                </button>
              </>
            ) : (
              <button className="btn-ghost text-xs text-danger" onClick={() => setConfirmEmpty(true)}>
                Empty the trash
              </button>
            )}
          </div>
        </>
      ) : (
        <p className="text-sm text-muted">Nothing in the trash.</p>
      )}
    </Section>
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

function ScheduleSection() {
  const client = useQueryClient();
  const schedule = useQuery({ queryKey: ["schedule"], queryFn: endpoints.schedule });
  const save = useMutation({
    mutationFn: endpoints.putSchedule,
    onSuccess: () => client.invalidateQueries({ queryKey: ["schedule"] }),
  });

  const data = schedule.data;
  if (!data) return null;
  const quiet = data.quiet_hours;
  const patch = (change: Partial<typeof data>) =>
    save.mutate({
      quiet_hours: quiet,
      per_host_serial: data.per_host_serial,
      full_recapture_days: data.full_recapture_days,
      digest_every_days: data.digest_every_days,
      ...change,
    });

  return (
    <Section
      title="Scheduling"
      description="Feeds carry their own intervals — these are the rules that apply to every
                  scheduled job. None of them affect a capture you start yourself."
    >
      <div className="space-y-4 text-sm">
        <div>
          <label className="flex items-center gap-2">
            <input
              type="checkbox"
              checked={quiet.enabled}
              onChange={(e) => patch({ quiet_hours: { ...quiet, enabled: e.target.checked } })}
            />
            Only run scheduled captures between
            <input
              className="field w-24 py-1"
              type="time"
              value={quiet.start}
              onChange={(e) => patch({ quiet_hours: { ...quiet, start: e.target.value } })}
            />
            and
            <input
              className="field w-24 py-1"
              type="time"
              value={quiet.end}
              onChange={(e) => patch({ quiet_hours: { ...quiet, end: e.target.value } })}
            />
          </label>
          <p className="hint mt-1.5">
            Off by default. Feeds are still polled outside the window — a poll is one
            conditional request — but anything new waits until the window opens rather than
            being lost. Times are this container's local time.
            {quiet.enabled && data.in_quiet_hours_now && (
              <strong className="ml-1 text-warn">Scheduled captures are paused right now.</strong>
            )}
          </p>
        </div>

        <div>
          <label className="flex items-center gap-2">
            <input
              type="checkbox"
              checked={data.per_host_serial}
              onChange={(e) => patch({ per_host_serial: e.target.checked })}
            />
            Never run two jobs against the same host at once
          </label>
          <p className="hint mt-1.5">
            Politeness rather than scheduling, so it applies to a capture you started by hand
            too. Two simultaneous crawls of one blog is what gets an archiver rate-limited.
          </p>
        </div>

        <div>
          <label className="flex items-center gap-2">
            Re-capture every site in full every
            <select
              className="field w-auto py-1"
              value={data.full_recapture_days}
              onChange={(e) => patch({ full_recapture_days: Number(e.target.value) })}
            >
              <option value={0}>never</option>
              <option value={7}>week</option>
              <option value={30}>month</option>
              <option value={90}>quarter</option>
              <option value={365}>year</option>
            </select>
          </label>
          <p className="hint mt-1.5">
            Off deliberately. A monthly full re-capture of a 3 GB archive is about 38 GB a year
            and mostly re-stores what you already have; feed capture covers new posts at a few
            percent of that. Turn this on only if you need to detect edits to existing pages.
          </p>
        </div>

        <div>
          <label className="flex items-center gap-2">
            Send a summary of what happened every
            <select
              className="field w-auto py-1"
              value={data.digest_every_days}
              onChange={(e) => patch({ digest_every_days: Number(e.target.value) })}
            >
              <option value={0}>never</option>
              <option value={1}>day</option>
              <option value={7}>week</option>
              <option value={30}>month</option>
            </select>
          </label>
          <p className="hint mt-1.5">
            Mostly a report of what has <em>not</em> happened: sites nothing has captured in a
            month, feeds that are polling and returning nothing, credentials about to expire.
            Goes to your notification targets; the same report is on the dashboard whether or
            not you have any, so turning this off loses the push and nothing else.
          </p>
        </div>
      </div>
    </Section>
  );
}

function NotificationsSection() {
  const client = useQueryClient();
  const settings = useQuery({ queryKey: ["notifications"], queryFn: endpoints.notifications });
  const [draft, setDraft] = useState("");
  const [result, setResult] = useState<string | null>(null);

  const save = useMutation({
    mutationFn: endpoints.putNotifications,
    onSuccess: () => client.invalidateQueries({ queryKey: ["notifications"] }),
  });
  const test = useMutation({
    mutationFn: endpoints.testNotifications,
    onSuccess: (r) =>
      setResult(
        r.problems.length
          ? `${r.delivered} of ${r.targets} delivered. ${r.problems.join("; ")}`
          : `Delivered to ${r.delivered} of ${r.targets} target(s).`,
      ),
    onError: (err) => setResult((err as ApiError).message),
  });

  const data = settings.data;
  if (!data) return null;

  return (
    <Section
      title="Notifications"
      description="New content in an archive is worth a push, and so is a feed that quietly
                  stopped working."
    >
      <div className="space-y-5 text-sm">
        <div className="space-y-2">
          {data.targets.map((target, index) => (
            <div key={`${target.url}-${index}`} className="flex items-center gap-2">
              <input
                type="checkbox"
                checked={target.enabled}
                onChange={(e) =>
                  save.mutate({
                    targets: data.targets.map((t, i) =>
                      i === index ? { ...t, enabled: e.target.checked } : t,
                    ),
                  })
                }
              />
              <code className="min-w-0 flex-1 truncate rounded bg-raised px-2 py-1 text-xs">
                {target.url}
              </code>
              <button
                className="btn-ghost px-2 text-xs text-danger"
                onClick={() =>
                  save.mutate({ targets: data.targets.filter((_, i) => i !== index) })
                }
              >
                Remove
              </button>
            </div>
          ))}

          <div className="flex gap-2">
            <input
              className="field min-w-0 flex-1 py-1 font-mono text-xs"
              placeholder="ntfy://ntfy.sh/my-topic, https://my-webhook/..., discord://id/token"
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              aria-label="Notification target"
            />
            <button
              className="btn-ghost text-xs"
              disabled={!draft.trim()}
              onClick={() => {
                save.mutate({
                  targets: [...data.targets, { url: draft.trim(), enabled: true, label: "" }],
                });
                setDraft("");
              }}
            >
              Add
            </button>
            <button
              className="btn-ghost text-xs"
              disabled={!data.targets.length || test.isPending}
              onClick={() => test.mutate()}
            >
              {test.isPending && <Spinner className="mr-1 h-3 w-3" />}
              Send a test
            </button>
          </div>
          <p className="hint">
            An <code>ntfy://</code> URL or an ntfy.sh address goes to ntfy; any other{" "}
            <code>http(s)://</code> gets a JSON POST, which is what Discord, Slack, Gotify and
            Home Assistant webhooks expect. Anything else is handed to Apprise
            {data.apprise_available ? "" : ", which is not installed in this build"}.
          </p>
          {result && <Alert kind="info">{result}</Alert>}
        </div>

        <div>
          <p className="label">Tell me about</p>
          <div className="mt-2 grid gap-1.5 sm:grid-cols-2">
            {Object.entries(data.labels).map(([event, label]) => (
              <label key={event} className="flex items-center gap-2 text-xs">
                <input
                  type="checkbox"
                  checked={data.events[event] ?? false}
                  onChange={(e) => save.mutate({ events: { [event]: e.target.checked } })}
                />
                {label}
              </label>
            ))}
          </div>
        </div>
      </div>
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
