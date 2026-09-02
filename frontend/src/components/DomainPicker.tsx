import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";

import {
  type ApiError,
  type DiscoveredHost,
  type DiscoverySummary,
  type HostRule,
  type ScopePreview,
  endpoints,
} from "../lib/api";
import { bytes } from "../lib/format";
import { Matches, MatchesFootnote, usePatternMatches } from "./PatternMatches";
import { Alert, EmptyState, Spinner } from "./ui";

/**
 * The domain picker.
 *
 * Two independent checkboxes per host is the whole point: you want an image
 * CDN's pictures without treating it as a site to crawl. Everything unknown
 * starts off, so a crawl can never wander onto a blog that merely appeared in
 * a sidebar.
 */
/** Two pattern lists holding the same patterns, order included. */
function same(a: string[], b: string[]) {
  return a.length === b.length && a.every((value, i) => value === b[i]);
}

export function DomainPicker({ siteId, onChanged }: { siteId: number; onChanged?: () => void }) {
  const client = useQueryClient();
  const [draft, setDraft] = useState<Record<string, HostRule> | null>(null);
  const [rejects, setRejects] = useState<string[] | null>(null);
  // Instance-wide patterns this site is excused from. Null while unloaded, so
  // an un-edited picker cannot post an empty list and silently re-apply a
  // rule somebody had turned off here.
  const [excepted, setExcepted] = useState<string[] | null>(null);
  const [cap, setCap] = useState<number | null | undefined>(undefined);
  // undefined = untouched, so the saved value shows through until somebody
  // actually changes it. A plain `useState(true)` would quietly turn robots
  // back on for any site that had it off, on the next unrelated scope save.
  const [robots, setRobots] = useState<boolean | undefined>(undefined);
  const [saved, setSaved] = useState(false);

  const discovery = useQuery({
    queryKey: ["discovery", siteId],
    queryFn: () => endpoints.discovery(siteId),
  });
  const scope = useQuery({ queryKey: ["scope", siteId], queryFn: () => endpoints.scope(siteId) });

  // The draft starts from what is saved, so an un-edited picker shows exactly
  // the current scope rather than a fresh set of defaults.
  useEffect(() => {
    if (!discovery.data?.hosts || !scope.data || draft) return;
    const initial: Record<string, HostRule> = {};
    for (const host of discovery.data.hosts) {
      initial[host.host] = {
        host: host.host,
        crawl_pages: host.crawl_pages,
        fetch_assets: host.fetch_assets,
        path_prefix: null,
        allow_extensionless: host.allow_extensionless,
      };
    }
    setDraft(initial);
    setRejects(scope.data.reject_patterns);
    setExcepted(scope.data.global_reject_exceptions);
    setCap(scope.data.max_pages);
  }, [discovery.data, scope.data, draft]);

  const save = useMutation({
    mutationFn: () =>
      endpoints.putScope(siteId, {
        hosts: Object.values(draft ?? {}),
        reject_patterns: rejects ?? [],
        global_reject_exceptions: excepted ?? scope.data?.global_reject_exceptions ?? [],
        obey_robots: robots ?? scope.data?.obey_robots ?? true,
        max_pages: cap === undefined ? (scope.data?.max_pages ?? null) : cap,
        max_bytes: scope.data?.max_bytes ?? null,
        politeness: scope.data?.politeness ?? {},
      }),
    onSuccess: async () => {
      setSaved(true);
      await Promise.all([
        client.invalidateQueries({ queryKey: ["scope", siteId] }),
        client.invalidateQueries({ queryKey: ["site", siteId] }),
        client.invalidateQueries({ queryKey: ["preview", siteId] }),
      ]);
      onChanged?.();
    },
  });

  const fingerprint = discovery.data?.discovery?.summary.fingerprint ?? null;
  const preset = fingerprint?.preset ?? null;
  // Variants of the detected preset. Offered as their own buttons rather than
  // hidden behind a menu, because the point of a variant is that you try it,
  // capture, and switch back — and each preset retires what the other adds, so
  // switching is a real comparison rather than an accumulation.
  const alternatives = fingerprint?.alternatives ?? [];
  const applyPreset = useMutation({
    mutationFn: (presetId: string) => endpoints.applyPreset(siteId, presetId),
    onSuccess: async () => {
      setDraft(null);
      await client.invalidateQueries({ queryKey: ["discovery", siteId] });
      await client.invalidateQueries({ queryKey: ["scope", siteId] });
      onChanged?.();
    },
  });

  const dirty = useMemo(() => {
    if (!draft || !discovery.data) return false;
    // The robots toggle counts too, or flipping it alone leaves the page
    // looking unchanged while holding an unsaved change. Same for both
    // pattern lists: adding a skip pattern, or switching an inherited one off
    // for this site, is a change somebody can lose by navigating away.
    if (robots !== undefined && robots !== (scope.data?.obey_robots ?? true)) return true;
    if (rejects && !same(rejects, scope.data?.reject_patterns ?? [])) return true;
    if (excepted && !same(excepted, scope.data?.global_reject_exceptions ?? [])) return true;
    return discovery.data.hosts.some(
      (h) =>
        draft[h.host]?.crawl_pages !== h.crawl_pages ||
        draft[h.host]?.fetch_assets !== h.fetch_assets,
    );
  }, [draft, discovery.data, robots, rejects, excepted, scope.data]);

  if (discovery.isLoading) return <Spinner className="h-5 w-5 text-muted" />;

  if (!discovery.data?.discovery) {
    return (
      <NotYetDiscovered siteId={siteId} onDone={onChanged} browser={discovery.data?.browser} />
    );
  }

  const summary = discovery.data.discovery.summary;
  const hosts = discovery.data.hosts;
  // A host with no draft entry yet still has to end up a complete rule, or a
  // partial object reaches the API and the scope silently loses its host name.
  const blank = (host: string): HostRule => ({
    host,
    crawl_pages: false,
    fetch_assets: false,
    path_prefix: null,
    allow_extensionless: false,
  });

  const set = (host: string, patch: Partial<HostRule>) => {
    setSaved(false);
    setDraft((prev) =>
      prev ? { ...prev, [host]: { ...(prev[host] ?? blank(host)), ...patch } } : prev,
    );
  };

  const bulk = (match: (h: DiscoveredHost) => boolean, patch: Partial<HostRule>) => {
    setSaved(false);
    setDraft((prev) => {
      if (!prev) return prev;
      const next = { ...prev };
      for (const host of hosts.filter(match)) {
        next[host.host] = { ...(next[host.host] ?? blank(host.host)), ...patch };
      }
      return next;
    });
  };

  return (
    <div className="space-y-4">
      <Found summary={summary} />

      {summary.warnings?.map((warning) => (
        <Alert kind="warn" key={warning}>
          {warning}
        </Alert>
      ))}

      {(discovery.data.diff?.new_hosts?.length ?? 0) > 0 && (
        <Alert kind="info" title="New since the last run">
          {discovery.data.diff?.new_hosts.join(", ")}
        </Alert>
      )}

      <div className="flex flex-wrap items-center gap-2 text-xs">
        <span className="text-muted">Select:</span>
        <button
          className="btn-ghost px-2 py-1 text-xs"
          onClick={() => bulk((h) => h.role === "images" || h.role === "cdn", { fetch_assets: true })}
        >
          all images and CDNs
        </button>
        <button
          className="btn-ghost px-2 py-1 text-xs"
          onClick={() =>
            bulk((h) => h.role === "analytics" || h.role === "ads", {
              fetch_assets: false,
              crawl_pages: false,
            })
          }
        >
          no analytics
        </button>
        <button
          className="btn-ghost px-2 py-1 text-xs"
          onClick={() => bulk((h) => !h.is_seed_host, { fetch_assets: false, crawl_pages: false })}
        >
          nothing but the site itself
        </button>
        {preset && (
          <button
            className="btn-ghost px-2 py-1 text-xs"
            onClick={() => applyPreset.mutate(preset.id)}
            disabled={applyPreset.isPending}
            title={preset.notes}
          >
            {applyPreset.isPending && applyPreset.variables === preset.id && <Spinner />}
            apply the {preset.name} preset
          </button>
        )}
        {alternatives.map((alt) => (
          <button
            key={alt.id}
            className="btn-ghost px-2 py-1 text-xs"
            onClick={() => applyPreset.mutate(alt.id)}
            disabled={applyPreset.isPending}
            title={alt.notes}
          >
            {applyPreset.isPending && applyPreset.variables === alt.id && <Spinner />}
            apply {alt.name}
          </button>
        ))}
      </div>

      <div className="card overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="border-b border-border text-left text-xs text-muted">
            <tr>
              <th className="px-3 py-2 font-medium">Host</th>
              <th className="px-3 py-2 text-right font-medium">Links</th>
              <th className="px-3 py-2 text-right font-medium">Assets</th>
              <th className="px-3 py-2 font-medium">Role</th>
              <th className="px-3 py-2 text-center font-medium">
                Crawl
                <span className="block font-normal normal-case">follow its pages</span>
              </th>
              <th className="px-3 py-2 text-center font-medium">
                Assets
                <span className="block font-normal normal-case">fetch its files</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {hosts.map((host) => (
              <HostRow
                key={host.host}
                host={host}
                rule={draft?.[host.host]}
                onChange={(patch) => set(host.host, patch)}
              />
            ))}
          </tbody>
        </table>
      </div>

      <RejectPatterns
        siteId={siteId}
        patterns={rejects ?? []}
        global={scope.data?.global_reject_patterns ?? []}
        excepted={excepted ?? scope.data?.global_reject_exceptions ?? []}
        onChange={(next) => {
          setSaved(false);
          setRejects(next);
        }}
        onExcept={(next) => {
          setSaved(false);
          setExcepted(next);
        }}
      />

      <CrawlCap
        value={cap === undefined ? (scope.data?.max_pages ?? null) : cap}
        onChange={(next) => {
          setSaved(false);
          setCap(next);
        }}
      />

      <ObeyRobots
        value={robots ?? scope.data?.obey_robots ?? true}
        disallowed={discovery.data?.discovery?.summary?.robots?.disallowed ?? []}
        onChange={(next) => {
          setSaved(false);
          setRobots(next);
        }}
      />

      {save.error && <Alert kind="error">{(save.error as ApiError).message}</Alert>}

      <div className="flex items-center gap-3">
        <button className="btn-primary" onClick={() => save.mutate()} disabled={save.isPending}>
          {save.isPending && <Spinner />}
          Save scope
        </button>
        {dirty && !saved && <span className="text-xs text-warn">Unsaved changes</span>}
        {saved && <span className="text-xs text-ok">Saved</span>}
        <Rediscover
          siteId={siteId}
          onDone={() => setDraft(null)}
          browser={discovery.data.browser}
        />
      </div>

      <Preview siteId={siteId} onApplyPreset={(id) => applyPreset.mutate(id)} />
    </div>
  );
}

function HostRow({
  host,
  rule,
  onChange,
}: {
  host: DiscoveredHost;
  rule: HostRule | undefined;
  onChange: (patch: Partial<HostRule>) => void;
}) {
  const [open, setOpen] = useState(false);
  const roleTone =
    {
      self: "text-accent",
      // The same site under another name, so it reads like the seed host
      // rather than like a third party — which is the whole point of telling
      // them apart.
      alias: "text-accent",
      analytics: "text-danger",
      images: "text-ok",
      cdn: "text-ok",
    }[host.role] ?? "text-muted";

  return (
    <>
      <tr className="border-b border-border last:border-0">
        <td className="px-3 py-2">
          <button className="text-left font-mono text-xs hover:underline" onClick={() => setOpen((v) => !v)}>
            {host.host}
          </button>
          {host.is_seed_host && (
            <span className="ml-2 rounded bg-accent/15 px-1.5 py-0.5 text-[10px] text-accent">
              this site
            </span>
          )}
          <span className="block text-[11px] text-muted">{host.registrable}</span>
        </td>
        <td className="px-3 py-2 text-right tabular-nums">{host.link_refs.toLocaleString()}</td>
        <td className="px-3 py-2 text-right tabular-nums">{host.asset_refs.toLocaleString()}</td>
        <td className={`px-3 py-2 text-xs ${roleTone}`}>{host.role}</td>
        <td className="px-3 py-2 text-center">
          <input
            type="checkbox"
            checked={rule?.crawl_pages ?? false}
            onChange={(e) => onChange({ crawl_pages: e.target.checked })}
            aria-label={`Crawl pages on ${host.host}`}
          />
        </td>
        <td className="px-3 py-2 text-center">
          <input
            type="checkbox"
            checked={rule?.fetch_assets ?? false}
            onChange={(e) => onChange({ fetch_assets: e.target.checked })}
            aria-label={`Fetch assets from ${host.host}`}
          />
        </td>
      </tr>
      {open && (
        <tr className="border-b border-border bg-raised/40">
          <td colSpan={6} className="px-3 py-2">
            {rule?.fetch_assets && !rule?.crawl_pages && (
              <label className="mb-2 flex items-center gap-2 text-xs">
                <input
                  type="checkbox"
                  checked={rule?.allow_extensionless ?? false}
                  onChange={(e) => onChange({ allow_extensionless: e.target.checked })}
                />
                Allow URLs with no file extension
                <span className="text-muted">
                  — needed for image proxies like <code>…/blogger_img_proxy/AEn0k_…</code>, at the
                  cost of possibly fetching a page from this host.
                </span>
              </label>
            )}
            <p className="mb-1 text-[11px] uppercase text-muted">Sample URLs</p>
            <ul className="space-y-0.5 font-mono text-[11px]">
              {host.sample_urls.length ? (
                host.sample_urls.map((url) => (
                  <li key={url} className="truncate text-muted">
                    {url}
                  </li>
                ))
              ) : (
                <li className="text-muted">none recorded</li>
              )}
            </ul>
          </td>
        </tr>
      )}
    </>
  );
}

/**
 * The stop switch.
 *
 * Called `max_pages` everywhere in the code and the API, and it does not count
 * pages: the supervisor counts every `url` event, which includes each image,
 * stylesheet and font. On a photo blog that is three or four times the post
 * count, so a cap set as though it meant pages stops the crawl a quarter of
 * the way in. The name is stuck; the label does not have to repeat the
 * mistake.
 */
/**
 * Whether the crawl honours robots.txt.
 *
 * It was in the scope model and in both engines, and nowhere in the UI — the
 * save call passed the stored value straight back, so the only way to change
 * it was the API. That mattered more than it looks: on Blogger it is the
 * switch governing everything under /search, which is where the Older-posts
 * trail lives, and advice to "turn it off" pointed at a control that did not
 * exist.
 *
 * The disallowed list comes from the robots.txt discovery already fetched, so
 * this says what is actually being excluded on *this* site rather than
 * explaining the idea of robots.txt in the abstract.
 */
function ObeyRobots({
  value,
  disallowed,
  onChange,
}: {
  value: boolean;
  disallowed: string[];
  onChange: (next: boolean) => void;
}) {
  return (
    <div className="card p-4">
      <label className="flex items-start gap-3">
        <input
          type="checkbox"
          className="mt-0.5"
          checked={value}
          onChange={(event) => onChange(event.target.checked)}
        />
        <span>
          <span className="text-sm font-medium">Obey robots.txt</span>
          <span className="hint mt-0.5 block">
            On by default, and the polite thing for a site you do not own. Turning it off is
            how you reach paths the site asks crawlers to skip — which on some platforms is
            where the archive pagination lives, so a link in the archived page can be dead
            with this on.
          </span>
        </span>
      </label>

      {disallowed.length > 0 && (
        <div className="mt-3 border-t border-border pt-3">
          <p className="hint">
            This site&apos;s robots.txt disallows {disallowed.length} path
            {disallowed.length === 1 ? "" : "s"}
            {value ? ", and they are being skipped:" : ", and they are being crawled anyway:"}
          </p>
          <div className="mt-1.5 flex flex-wrap gap-1.5">
            {disallowed.slice(0, 12).map((path) => (
              <code key={path} className="rounded bg-raised px-1.5 py-0.5 text-[11px]">
                {path}
              </code>
            ))}
            {disallowed.length > 12 && (
              <span className="text-[11px] text-muted">+{disallowed.length - 12} more</span>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

function CrawlCap({
  value,
  onChange,
}: {
  value: number | null;
  onChange: (next: number | null) => void;
}) {
  const [draft, setDraft] = useState(value === null ? "" : String(value));

  return (
    <div className="card p-4">
      <h3 className="text-sm font-medium">Stop after</h3>
      <p className="hint mt-0.5">
        Counted in <strong>URLs, not pages</strong> — every image and stylesheet is one. Empty
        for no limit. This is what stops a crawl that has found a corner of the site it can
        generate forever, like a paginated tag archive.
      </p>
      <div className="mt-3 flex items-center gap-2">
        <input
          className="input w-40 text-sm"
          type="number"
          min={0}
          step={1000}
          placeholder="no limit"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onBlur={() => {
            const trimmed = draft.trim();
            if (!trimmed) {
              onChange(null);
              return;
            }
            const next = Number(trimmed);
            if (Number.isFinite(next) && next >= 0) onChange(Math.round(next));
          }}
        />
        <span className="text-xs text-muted">URLs</span>
      </div>
    </div>
  );
}

/**
 * The two skip lists, in one panel because they answer one question.
 *
 * The instance-wide list is shown but not edited here — editing it here would
 * mean editing it for every site, which is not what somebody looking at one
 * site's scope is asking to do. What this panel *can* do is switch an entry
 * off for this site, because a rule that is right for the web in general and
 * wrong for one blog is otherwise a reason not to have the list at all.
 */
function RejectPatterns({
  siteId,
  patterns,
  global: globals,
  excepted,
  onChange,
  onExcept,
}: {
  siteId: number;
  patterns: string[];
  global: string[];
  excepted: string[];
  onChange: (next: string[]) => void;
  onExcept: (next: string[]) => void;
}) {
  const [value, setValue] = useState("");
  const off = new Set(excepted);
  const active = globals.filter((p) => !off.has(p)).length + patterns.length;
  // Against this site's own URLs. The same pattern can be doing all the work
  // on one blog and nothing on another, and a global count would hide that.
  const check = usePatternMatches([...globals, ...patterns], siteId);
  return (
    <div className="card p-4">
      <h3 className="text-sm font-medium">
        Skip URLs matching
        {/* The list is scrollable and a preset contributes a dozen or more, so
            "how many are on this site?" is not answerable by looking. Counts
            what is in force, which is why a switched-off global rule lowers
            it. */}
        {active > 0 && <span className="ml-2 font-normal text-muted">{active}</span>}
      </h3>
      <p className="hint mt-0.5">
        Regular expressions. Blogger's <code>[?&amp;]m=1</code> is the important one — it serves
        every post twice and rejecting the duplicate halves the crawl with no content loss.
      </p>

      {globals.length > 0 && (
        <div className="mt-3">
          <p className="text-xs font-medium text-muted">
            From Settings, on every site
            <span className="ml-2 font-normal">{globals.length}</span>
          </p>
          <ul className="mt-1 space-y-1">
            {globals.map((pattern) => {
              const disabled = off.has(pattern);
              return (
                <li key={pattern} className="flex items-center gap-3">
                  <code
                    className={`min-w-0 flex-1 truncate text-xs ${
                      disabled ? "text-muted line-through" : ""
                    }`}
                  >
                    {pattern}
                  </code>
                  {!disabled && <Matches check={check.data} pattern={pattern} />}
                  <button
                    className="shrink-0 text-xs text-muted hover:text-fg"
                    onClick={() =>
                      onExcept(
                        disabled
                          ? excepted.filter((p) => p !== pattern)
                          : [...excepted, pattern],
                      )
                    }
                  >
                    {disabled ? "use here" : "skip here"}
                  </button>
                </li>
              );
            })}
          </ul>
          <p className="hint mt-1">
            Edit these under Settings → Skip these URLs everywhere. Turning one off applies to this
            site only.
          </p>
        </div>
      )}

      <ul className="mt-3 space-y-1">
        {globals.length > 0 && patterns.length > 0 && (
          <li className="text-xs font-medium text-muted">This site only</li>
        )}
        {patterns.map((pattern) => (
          <li key={pattern} className="flex items-center gap-3">
            <code className="min-w-0 flex-1 truncate text-xs">{pattern}</code>
            <Matches check={check.data} pattern={pattern} />
            <button
              className="shrink-0 text-xs text-muted hover:text-danger"
              onClick={() => onChange(patterns.filter((p) => p !== pattern))}
            >
              remove
            </button>
          </li>
        ))}
        {patterns.length === 0 && globals.length === 0 && (
          <li className="text-xs text-muted">No patterns.</li>
        )}
      </ul>
      <form
        className="mt-3 flex gap-2"
        onSubmit={(event) => {
          event.preventDefault();
          const trimmed = value.trim();
          if (trimmed && !patterns.includes(trimmed)) onChange([...patterns, trimmed]);
          setValue("");
        }}
      >
        <input
          className="field text-xs"
          placeholder="[?&amp;]m=1"
          value={value}
          onChange={(event) => setValue(event.target.value)}
        />
        <button className="btn-ghost text-xs">Add</button>
      </form>
      <MatchesFootnote check={check.data} />
    </div>
  );
}

function Preview({
  siteId,
  onApplyPreset,
}: {
  siteId: number;
  onApplyPreset?: (preset: string) => void;
}) {
  const preview = useQuery({
    queryKey: ["preview", siteId],
    queryFn: () => endpoints.scopePreview(siteId),
  });
  if (!preview.data) return null;
  const data = preview.data;

  return (
    <div className="card p-4">
      <h3 className="text-sm font-medium">If you captured now</h3>
      <dl className="mt-2 grid gap-x-8 gap-y-1 text-sm sm:grid-cols-2">
        <Row label="Pages to crawl" value={`~${data.pages_to_crawl.toLocaleString()}`} />
        <Row label="Asset hosts allowed" value={String(data.asset_hosts.length)} />
        <Row
          label="Excluded by pattern"
          value={data.excluded_by_pattern.toLocaleString()}
        />
        <Row label="Rough size" value={bytes(data.estimated_bytes)} />
        <Row label="Rough time" value={roughTime(data.estimated_seconds)} />
      </dl>
      <Pagination outlook={data.pagination} onApply={onApplyPreset} />
      {data.notes.map((note) => (
        <p key={note} className="hint mt-2">
          {note}
        </p>
      ))}
    </div>
  );
}

/** Seconds as something a person can weigh a decision against. */
function roughTime(seconds: number): string {
  if (!seconds) return "—";
  if (seconds < 90) return `${Math.round(seconds)}s`;
  const minutes = seconds / 60;
  if (minutes < 90) return `${Math.round(minutes)} min`;
  const hours = minutes / 60;
  return hours < 48 ? `${Math.round(hours)} h` : `${Math.round(hours / 24)} days`;
}

/**
 * What Blogger's Older-posts trail will cost this blog.
 *
 * It is kept by default because dropping it leaves a dead link at the bottom
 * of every archived page, and on the blog that was first measured against it
 * cost 86 fetches — an obviously good trade. The cost is **quadratic in post
 * count**, which that measurement could not show: 1.2 index URLs per post at
 * 71 posts, 4.6 at 371, and 71.9 at 2,855, where it was 92% of a capture that
 * ran for four days without finishing.
 *
 * So the trade is still offered, with the number attached.
 */
function Pagination({
  outlook,
  onApply,
}: {
  outlook: ScopePreview["pagination"];
  onApply?: (preset: string) => void;
}) {
  if (!outlook) return null;
  if (outlook.skipped) {
    return (
      <p className="hint mt-2">
        The Older-posts trail is skipped by this scope — roughly{" "}
        {outlook.estimated_urls.toLocaleString()} index fetches it does not have to make. Run the
        companion pass after the capture and those links still work in replay.
      </p>
    );
  }
  const tone = outlook.recommend_lean ? "warn" : "ok";
  return (
    <Alert kind={tone} title={`Older-posts trail: about ${outlook.estimated_urls.toLocaleString()} extra fetches`}>
      This blog has around {outlook.posts.toLocaleString()} posts, and Blogger gives every one of
      them its own address into the index. The cost grows with the square of the post count —
      measured at{" "}
      {outlook.measured
        .map((m) => `${m.urls.toLocaleString()} on ${m.posts.toLocaleString()} posts`)
        .join(", ")}
      .{" "}
      {outlook.recommend_lean ? (
        <>
          At this size the trail is most of the capture. The <strong>lean preset</strong> leaves it
          out and a cheap second pass fetches it afterwards, so Older and Newer Posts still resolve
          in replay.
          {onApply && (
            <button
              className="btn-ghost ml-2 px-2 py-0.5 text-xs"
              onClick={() => onApply("blogger-lean")}
            >
              apply the lean preset
            </button>
          )}
        </>
      ) : (
        <>At this size it is worth keeping: it is what makes Older Posts work in replay.</>
      )}
    </Alert>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex justify-between gap-4 border-b border-border py-1 last:border-0">
      <dt className="text-muted">{label}</dt>
      <dd className="tabular-nums">{value}</dd>
    </div>
  );
}

function Found({ summary }: { summary: DiscoverySummary }) {
  const fp = summary.fingerprint;
  return (
    <div className="flex flex-wrap gap-x-6 gap-y-1 text-xs text-muted">
      <span>
        Platform:{" "}
        <strong className="text-fg">
          {fp.preset?.name ?? fp.platform}
          {fp.confidence === "weak" && " (probably)"}
        </strong>
      </span>
      <span>
        {summary.urls_from_sitemaps.toLocaleString()} URLs from {summary.sitemaps.length} sitemap(s)
      </span>
      <span>
        {summary.urls_from_feeds.toLocaleString()} from {summary.feeds.length} feed(s)
      </span>
      <span>{summary.pages_fetched} pages sampled</span>
      {(summary.browser?.rendered_pages ?? 0) > 0 && (
        <span>
          <strong className="text-fg">{summary.browser?.rendered_pages} rendered</strong>,{" "}
          {summary.browser?.requests_seen.toLocaleString()} requests seen
        </span>
      )}
    </div>
  );
}

function NotYetDiscovered({
  siteId,
  onDone,
  browser,
}: {
  siteId: number;
  onDone?: () => void;
  browser?: { available: boolean; reason: string };
}) {
  return (
    <EmptyState
      title="Not indexed yet"
      action={
        <Rediscover
          siteId={siteId}
          onDone={onDone}
          label="Index this site"
          primary
          browser={browser}
        />
      }
    >
      Indexing reads the sitemap, the feeds and a sample of pages to work out which domains this
      site pulls from. It writes nothing and can be re-run any time.
    </EmptyState>
  );
}

export function Rediscover({
  siteId,
  onDone,
  label = "Re-index",
  primary = false,
  browser,
}: {
  siteId: number;
  onDone?: () => void;
  label?: string;
  primary?: boolean;
  /** Whether this install can render, so the option is only offered if it works. */
  browser?: { available: boolean; reason: string };
}) {
  const client = useQueryClient();
  const [jobId, setJobId] = useState<number | null>(null);
  const [useBrowser, setUseBrowser] = useState(false);

  const start = useMutation({
    mutationFn: () => endpoints.discover(siteId, useBrowser),
    onSuccess: (result) => setJobId(result.job_id),
  });

  // Discovery is short, so a poll is simpler than a stream and just as timely.
  useQuery({
    queryKey: ["discovery-job", jobId],
    queryFn: async () => {
      const job = await endpoints.job(jobId as number);
      if (job.status !== "queued" && job.status !== "running") {
        setJobId(null);
        await client.invalidateQueries({ queryKey: ["discovery", siteId] });
        await client.invalidateQueries({ queryKey: ["scope", siteId] });
        await client.invalidateQueries({ queryKey: ["site", siteId] });
        onDone?.();
      }
      return job;
    },
    enabled: jobId !== null,
    refetchInterval: 1000,
  });

  const running = jobId !== null || start.isPending;
  return (
    <span className="inline-flex flex-wrap items-center gap-2">
      <button
        className={primary ? "btn-primary" : "btn-ghost text-xs"}
        onClick={() => start.mutate()}
        disabled={running}
      >
        {running && <Spinner />}
        {running ? "Indexing…" : label}
      </button>
      {browser?.available && (
        <label
          className="flex items-center gap-1 text-xs text-muted"
          title={
            "Loads each sampled page in a real browser and records every request it makes. " +
            "Finds hosts that only JavaScript names, and pages behind infinite scroll — " +
            "and takes seconds per page rather than milliseconds."
          }
        >
          <input
            type="checkbox"
            checked={useBrowser}
            onChange={(e) => setUseBrowser(e.target.checked)}
            disabled={running}
          />
          in a browser
        </label>
      )}
    </span>
  );
}
