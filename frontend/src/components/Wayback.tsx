import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import {
  ApiError,
  endpoints,
  type UrlGroup,
  type WaybackOptions,
  type WaybackState,
  type WaybackSurvey,
} from "../lib/api";
import { dateTime } from "../lib/format";
import { LiveLog } from "./LiveLog";
import { Alert, Field, PanelHeader, Spinner, useCollapsible } from "./ui";

/**
 * What the Wayback Machine already holds of this site (docs/19, stage 1).
 *
 * The survey asks archive.org only, never the site, so it is safe to run
 * against an origin that blocks crawlers. Its answer is the number the rest of
 * the feature is judged by: how many items an import could bring across, how
 * old they are, and how long fetching them would take.
 */
export function Wayback({ siteId }: { siteId: number }) {
  const { open, toggle } = useCollapsible(`wayback.${siteId}`, false);
  const client = useQueryClient();
  const [watching, setWatching] = useState<number | null>(null);

  const query = useQuery({
    queryKey: ["wayback", siteId],
    queryFn: () => endpoints.wayback(siteId),
    enabled: open,
  });
  const refresh = () => client.invalidateQueries({ queryKey: ["wayback", siteId] });

  const job = query.data?.job;
  const active = job && (job.status === "queued" || job.status === "running");
  // A survey started in another tab, or before a reload, is still worth following.
  useEffect(() => {
    if (active && watching === null && job) setWatching(job.id);
  }, [active, job, watching]);

  const start = useMutation({
    mutationFn: (fresh: boolean) => endpoints.startWaybackSurvey(siteId, fresh),
    onSuccess: (accepted) => {
      setWatching(accepted.job_id);
      void refresh();
    },
  });

  const data: WaybackState | undefined = query.data;

  return (
    <section className="card p-5">
      <PanelHeader
        title="Wayback Machine"
        hint="See how much of this site the Internet Archive already holds. The survey asks archive.org only and never contacts the site."
        open={open}
        onToggle={toggle}
      />

      {open && (
        <div className="mt-4 space-y-5">
          {query.isLoading && <Spinner className="h-5 w-5 text-muted" />}
          {query.error && <Alert kind="error">{(query.error as ApiError).message}</Alert>}

          {data && (
            <>
              <div className="flex flex-wrap items-center gap-2">
                <button
                  className="btn-primary"
                  disabled={start.isPending || Boolean(active)}
                  onClick={() => start.mutate(false)}
                >
                  {start.isPending && <Spinner />}
                  {data.survey ? "Run survey again" : "Run survey"}
                </button>
                {data.survey && (
                  <button
                    className="btn-ghost"
                    disabled={start.isPending || Boolean(active)}
                    onClick={() => start.mutate(true)}
                    title="Download the whole listing again instead of reusing saved pages"
                  >
                    Refresh listing
                  </button>
                )}
                <span className="hint">
                  archive.org is asked about one page every 10 seconds; a large site can take an
                  hour.
                </span>
              </div>
              {start.error && <Alert kind="error">{(start.error as ApiError).message}</Alert>}
              {job?.status === "failed" && !active && watching === null && (
                <Alert kind="warn">
                  The last survey stopped: {job.error}. Running it again continues from the pages
                  already listed.
                </Alert>
              )}

              {watching !== null && (
                <LiveLog
                  jobId={watching}
                  onFinished={() => void refresh()}
                  onDismiss={() => setWatching(null)}
                />
              )}

              {data.survey ? (
                <SurveyReport survey={data.survey} liveItems={data.options.live_items} />
              ) : (
                !active && (
                  <p className="hint">
                    No survey yet. Set up URL groups below first if the site serves one item under
                    several addresses.
                  </p>
                )
              )}

              <OptionsForm siteId={siteId} options={data.options} />
            </>
          )}
        </div>
      )}
    </section>
  );
}

function n(value: number | undefined | null): string {
  return (value ?? 0).toLocaleString();
}

function waybackDate(ts: string | null | undefined): string {
  if (!ts || ts.length < 8) return "—";
  return `${ts.slice(0, 4)}-${ts.slice(4, 6)}-${ts.slice(6, 8)}`;
}

function SurveyReport({ survey, liveItems }: { survey: WaybackSurvey; liveItems: number | null }) {
  const groups = survey.groups ?? [];
  const selected = survey.selected ?? { total: 0, pages: 0, assets: 0 };
  // With groups, an "item" is a group item (a story); without, it is a page.
  const items = groups.length > 0 ? groups.reduce((sum, g) => sum + g.items, 0) : selected.pages;
  const coverage = liveItems && liveItems > 0 ? Math.min(items / liveItems, 1) : null;
  const pages = survey.freshness?.pages ?? {};
  const maxYear = Math.max(1, ...Object.values(pages));
  const rejected = Object.entries(survey.out_of_scope ?? {}).slice(0, 6);
  const sections = survey.sections ?? [];
  const boilerplate =
    (survey.boilerplate?.rate_limit_digests.length ?? 0) +
    Object.keys(survey.boilerplate?.shared_digests ?? {}).length;

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Figure
          label={groups.length > 0 ? "Items in Wayback" : "Pages in Wayback"}
          value={n(items)}
          sub={coverage !== null ? `${Math.round(coverage * 100)}% of ${n(liveItems)}` : undefined}
        />
        <Figure
          label="Import would fetch"
          value={n(selected.total)}
          sub={`${n(selected.assets)} assets`}
        />
        <Figure
          label="Time at current pace"
          value={duration(survey.estimate?.hours ?? 0)}
          sub={`${survey.estimate?.rate_per_min ?? 0} requests/min`}
        />
        <Figure
          label="Error pages skipped"
          value={n(survey.urls_only_error_pages)}
          sub={`${boilerplate} repeated page fingerprint(s)`}
        />
      </div>

      <p className="hint">
        Surveyed {survey.generated_at ? dateTime(survey.generated_at) : "earlier"} from{" "}
        {n(survey.rows)} archive rows across {(survey.hosts ?? []).join(", ") || "the site"}. Copies
        range from {waybackDate(survey.freshness?.oldest)} to{" "}
        {waybackDate(survey.freshness?.newest)}.
      </p>

      {Object.keys(pages).length > 0 && (
        <div>
          <h3 className="label">Newest copy of each page, by year</h3>
          <div className="space-y-1">
            {Object.entries(pages).map(([year, count]) => (
              <div key={year} className="flex items-center gap-2 text-xs">
                <span className="w-10 text-muted">{year}</span>
                <div className="h-3 flex-1 rounded bg-raised">
                  <div
                    className="h-3 rounded bg-accent"
                    style={{
                      width: `${Math.max((count / maxYear) * 100, 1)}%`,
                    }}
                  />
                </div>
                <span className="w-16 text-right tabular-nums">{n(count)}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {groups.length > 0 && (
        <div>
          <h3 className="label">URL groups</h3>
          <table className="w-full text-xs">
            <thead className="text-left text-muted">
              <tr>
                <th className="py-1 font-normal">Group</th>
                <th className="py-1 pl-4 text-right font-normal">Items</th>
                <th className="py-1 pl-4 text-right font-normal">Fetches</th>
                <th className="py-1 pl-4 text-right font-normal">Variants skipped</th>
              </tr>
            </thead>
            <tbody>
              {groups.map((g) => (
                <tr key={g.name} className="border-t border-border align-top">
                  <td className="py-1.5">
                    <div>{g.name}</div>
                    <div className="text-muted">
                      {Object.entries(g.views)
                        .map(([view, count]) => `${view} ${n(count)}`)
                        .join(" · ")}
                    </div>
                  </td>
                  <td className="py-1.5 pl-4 text-right tabular-nums">{n(g.items)}</td>
                  <td className="py-1.5 pl-4 text-right tabular-nums">{n(g.fetches)}</td>
                  <td className="py-1.5 pl-4 text-right tabular-nums">{n(g.skipped_variants)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {sections.length > 0 && (
        <div>
          <h3 className="label">Biggest parts of the import</h3>
          <ul className="space-y-0.5 text-xs">
            {sections.map((s) => (
              <li key={`${s.section}|${s.kind}`} className="flex justify-between gap-3">
                <span className="truncate">
                  <code>{s.section}</code>
                  {s.kind && <span className="ml-2 text-muted">{s.kind}</span>}
                </span>
                <span className="tabular-nums">{n(s.count)}</span>
              </li>
            ))}
          </ul>
          <p className="hint mt-1.5">
            Something here you don't want? Add a reject pattern to the site's scope and run the
            survey again.
          </p>
        </div>
      )}

      {survey.discovered && (
        <p className="text-sm">
          Of {n(survey.discovered.discovered)} URLs found by the last discovery,{" "}
          <strong>{n(survey.discovered.in_wayback)}</strong> are in Wayback and{" "}
          <strong>{n(survey.discovered.missing_from_wayback)}</strong> would have to come from the
          site. Wayback also has {n(survey.discovered.wayback_only_pages)} pages discovery did not
          find, which may be deleted ones.
        </p>
      )}

      {rejected.length > 0 && (
        <div>
          <h3 className="label">Left out by the site's scope</h3>
          <ul className="space-y-0.5 text-xs">
            {rejected.map(([reason, count]) => (
              <li key={reason} className="flex justify-between gap-3">
                <code className="truncate">{reason}</code>
                <span className="tabular-nums text-muted">{n(count)} rows</span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

function duration(hours: number): string {
  if (hours < 1) return `${Math.max(Math.round(hours * 60), 1)} min`;
  if (hours < 48) return `${Math.round(hours * 10) / 10} h`;
  return `${Math.round(hours / 24)} days`;
}

function Figure({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="rounded-md border border-border p-3">
      <div className="text-xs text-muted">{label}</div>
      <div className="mt-1 text-lg font-medium tabular-nums">{value}</div>
      {sub && <div className="text-xs text-muted">{sub}</div>}
    </div>
  );
}

const EMPTY_GROUP: UrlGroup = { name: "", pattern: "", prefer: [], keep: [] };

function list(text: string): string[] {
  return text
    .split(",")
    .map((v) => v.trim())
    .filter(Boolean);
}

/**
 * URL groups, the rate-limit page, and the live item count.
 *
 * A URL group says several addresses are one item: fictionmania serves each
 * story as readtextstory, readxstory, readhtmlstory, readststory and details.
 * The pattern names the item with (?P<item>…) and the variant with (?P<view>…);
 * `prefer` picks one variant per item and `keep` adds ones always wanted.
 */
function OptionsForm({ siteId, options }: { siteId: number; options: WaybackOptions }) {
  const client = useQueryClient();
  const [groups, setGroups] = useState<UrlGroup[]>(options.url_groups);
  const [ratePage, setRatePage] = useState(options.rate_limit_page ?? "");
  const [liveItems, setLiveItems] = useState(options.live_items?.toString() ?? "");

  useEffect(() => {
    setGroups(options.url_groups);
    setRatePage(options.rate_limit_page ?? "");
    setLiveItems(options.live_items?.toString() ?? "");
  }, [options]);

  const save = useMutation({
    mutationFn: () =>
      endpoints.putWaybackOptions(siteId, {
        url_groups: groups.filter((g) => g.name || g.pattern),
        rate_limit_page: ratePage.trim() || null,
        live_items: liveItems.trim() ? Number(liveItems) : null,
      }),
    onSuccess: (data) => client.setQueryData(["wayback", siteId], data),
  });

  const update = (index: number, patch: Partial<UrlGroup>) =>
    setGroups((prev) => prev.map((g, i) => (i === index ? { ...g, ...patch } : g)));

  return (
    <div className="space-y-4 border-t border-border pt-4">
      <h3 className="text-sm font-medium">Survey settings</h3>

      <div className="space-y-3">
        {groups.map((group, index) => (
          <div key={index} className="space-y-2 rounded-md border border-border p-3">
            <div className="grid gap-2 sm:grid-cols-[10rem_1fr]">
              <input
                className="field"
                placeholder="Name, e.g. stories"
                value={group.name}
                onChange={(e) => update(index, { name: e.target.value })}
              />
              <input
                className="field font-mono text-xs"
                placeholder="/stories/(?P<view>\w+)\.html\?storyID=(?P<item>\d+)"
                value={group.pattern}
                onChange={(e) => update(index, { pattern: e.target.value })}
              />
            </div>
            <div className="grid gap-2 sm:grid-cols-2">
              <input
                className="field text-xs"
                placeholder="Prefer, in order: readtextstory, readxstory"
                value={group.prefer.join(", ")}
                onChange={(e) => update(index, { prefer: list(e.target.value) })}
              />
              <input
                className="field text-xs"
                placeholder="Always keep: details"
                value={group.keep.join(", ")}
                onChange={(e) => update(index, { keep: list(e.target.value) })}
              />
            </div>
            <button
              className="btn-ghost text-xs text-danger"
              onClick={() => setGroups((prev) => prev.filter((_, i) => i !== index))}
            >
              Remove group
            </button>
          </div>
        ))}
        <button
          className="btn-ghost text-xs"
          onClick={() => setGroups((prev) => [...prev, { ...EMPTY_GROUP }])}
        >
          Add a URL group
        </button>
        <p className="hint">
          A URL group treats several addresses as one item, so the import fetches one copy per item,
          not one per address. The pattern needs <code>(?P&lt;item&gt;…)</code>, and{" "}
          <code>(?P&lt;view&gt;…)</code> to choose between variants.
        </p>
      </div>

      <div className="grid gap-3 sm:grid-cols-2">
        <Field
          label="Rate-limit page"
          hint="A pattern for the error page the site shows instead of content when it is throttling, e.g. /err429\.html. Copies of it in Wayback are skipped."
        >
          <input
            className="field font-mono text-xs"
            value={ratePage}
            onChange={(e) => setRatePage(e.target.value)}
          />
        </Field>
        <Field
          label="Items on the live site"
          hint="Optional. Wayback cannot tell how many items the site has; enter it to see a coverage percentage."
        >
          <input
            className="field"
            inputMode="numeric"
            value={liveItems}
            onChange={(e) => setLiveItems(e.target.value.replace(/[^0-9]/g, ""))}
          />
        </Field>
      </div>

      <div className="flex items-center gap-2">
        <button className="btn-ghost" disabled={save.isPending} onClick={() => save.mutate()}>
          {save.isPending && <Spinner />}
          Save settings
        </button>
        {save.isSuccess && !save.isPending && (
          <span className="hint">Saved. Run the survey again to apply.</span>
        )}
      </div>
      {save.error && <Alert kind="error">{(save.error as ApiError).message}</Alert>}
    </div>
  );
}
