# 17 — Wayback Pre-fill (design, not built)

Status: **draft for review.** Nothing here is implemented.

The goal: when the Internet Archive's Wayback Machine already holds a decent copy of a site, bring that copy into Cairn and fetch only the gap, so a backup does not cost a full crawl. The motivating case is a site whose Wayback copy is good but incomplete, and whose **origin rate-limits hard**. Every request to the origin is expensive, and archive.org is the cheap source. That makes **wayback-first** the primary strategy. live-first is kept for sites where it's the other way round.

---

## The one decision that matters most: which source is scarce?

Two request budgets are in play: the origin site's and archive.org's. The design runs in one of two strategies depending on which budget is scarce.

| Strategy | When | What comes from Wayback | What comes from the live site |
|---|---|---|---|
| **wayback-first** (the motivating case) | Origin is rate-limited hard, blocking us, or dead; **the origin is the scarce budget** | Every in-scope URL whose snapshot passes the freshness rule | Only the gap: URLs Wayback lacks, or holds only stale copies of |
| **live-first** | Origin tolerates a polite crawl and archive.org is the one throttling | Only URLs the live site no longer serves: deleted posts, removed pages, dead assets | Everything it still serves |

Take a blog with 2,000 posts, 1,700 of them in Wayback. Under wayback-first, the origin sees ~300 page requests plus their assets instead of ~2,000. That's a sevenfold cut in exposure to its rate limiter. The 1,700 Wayback fetches cost time, not risk.

The survey (phase 1) costs a handful of archive.org requests and **zero origin requests**. It runs first either way and gives the user the numbers to choose with. The plan's headline figure is **origin requests needed**, because under this strategy that's the number that matters.

---

## Phases

```
1 Survey ──► 2 Plan ──► 3 Wayback import ──► 4 Live gap crawl
 (CDX list)  (local)     (slow, resumable)    (existing engines)
```

Each phase is useful alone, and they ship in that order (see [Milestones](#milestones)).

### 1. Survey — what does Wayback have?

One CDX prefix query per crawlable host, paged with a resume key:

```
https://web.archive.org/cdx/search/cdx
  ?url=<host>/&matchType=prefix
  &fl=urlkey,timestamp,original,mimetype,statuscode,digest,length
  &filter=statuscode:[23]..
  &showResumeKey=true&limit=5000
```

- **Pick the newest snapshot per URL locally.** `collapse=urlkey` keeps the *first* (oldest) row, not the newest. So rows are streamed and reduced in Python instead.
- **Page with `showResumeKey`, not `page=`.** `page=N` is reported to misbehave on prefix queries.
- **Apply the site's scope to every row.** That covers hosts, `path_prefix`, `reject_patterns` and the global skip list. Wayback holds every `?m=1`, `showComment=` and `replytocom=` variant a Blogger site ever had, and the site's reject patterns already exist to drop exactly those.
- **Normalise with SURT.** Use the `surt` package already in the venv, so `http://`/`https://`, `www.` and trailing-slash variants match the local index's keys.
- **Cache the result** at `<site>/import/wayback-survey.json` with the query time. Re-planning should not re-query. A "refresh survey" button re-queries on purpose.

**Output (shown to the user before anything is fetched):**

- URLs in Wayback, in scope: total, pages vs assets.
- Snapshot age distribution: newest, oldest, a histogram by year.
- Discovered URLs (sitemap, feeds, manual) **not** in Wayback. This is the gap a live crawl must cover.
- In Wayback but **not** discovered. These are likely deleted pages, which only Wayback can supply.
- Estimated Wayback fetches and wall-clock time for each strategy at the configured rate.

The coverage report alone answers "is this worth it?" for a few requests.

### 2. Plan — split the URL set (local, no network)

Every in-scope URL lands in exactly one bucket:

| Bucket | Rule |
|---|---|
| `skip` | Already in the site's own index with a capture at least as new as the Wayback snapshot |
| `wayback` | Strategy-dependent (below) |
| `live` | Everything else in scope |
| `always_live` | Matches the platform's volatile-page patterns: home, label/tag listings, archive/date pages, search, feeds. For Blogger these come from `discovery/platform.py`. **Against a rate-limited origin this bucket costs real budget**, so it's a user choice. The default is the home page only, and the rest are taken from Wayback when they are newer than the freshness cutoff. Listing pages are navigation; the posts they list are what is being preserved. |

**Freshness rule (wayback-first):** a snapshot counts as current if it is newer than a cutoff (default: none, so any age is accepted; user-settable, e.g. "2 years"). Blog posts rarely change after publication. Listings are handled by `always_live`, not by the cutoff.

**live-first** puts in `wayback` only the URLs the live crawl could not get. That means 404/410/gone in the most recent live capture, plus survey URLs never discovered live. So in live-first, phase 3 runs **after** phase 4.

**Ordering inside `wayback`:** HTML pages first, then CSS, then images, then everything else. If the budget runs out part-way, the result is readable text with missing pictures, not pictures with no text.

The plan is written to `<tmp>/wayback-plan.jsonl` and shown as counts per bucket.

### 3. Wayback import — the slow part

Implemented as a **new engine, `wayback-import`**, under the existing engine contract ([05](05-capture-engines.md)). Seeds are the `wayback` bucket with their chosen timestamps, and config carries pacing. That gets job progress, cancel, pause/resume, limits, the event stream and the conformance harness without new plumbing.

**Fetching:**

- `GET https://web.archive.org/web/<timestamp>id_/<original>`. The `id_` modifier returns the archived bytes without the toolbar or rewritten links.
- **Probed 2026-10-30.** The test was one `id_` fetch of an `example.com` snapshot, with and without `Accept-Encoding`:
  - **The body is the archived payload byte-for-byte**, still compressed: `Content-Encoding: gzip`, body starting `1f 8b`. This happened even when the request sent no `Accept-Encoding`. The SHA-1 of those raw bytes (base32) **equals the CDX `digest`**.
  - The original's `Content-Encoding` shows up as a plain `Content-Encoding` header, not as `X-Archive-Orig-Content-Encoding`. Every other original header comes back as `X-Archive-Orig-*`.
  - `Memento-Datetime` gives the real snapshot time.
- **So: read the raw stream and never let the client decode it.** With httpx, that means `response.aiter_raw()`, not `aiter_bytes()`; the discovery `Fetcher` decodes, so the importer needs its own read path. Store the bytes untouched with their `Content-Encoding` header. That is how wget writes its WARCs too, and pywb decodes on replay.
- **Rebuilding the HTTP header block:** the real `Content-Type` and `Content-Encoding`, plus every `X-Archive-Orig-*` header with its prefix stripped. Drop `Transfer-Encoding` and `Connection` (hop-by-hop). Recompute `Content-Length` from the body, because the original was often chunked and had none.
- Compute the payload SHA-1 and compare it with the CDX `digest`. On a mismatch, keep the record but flag it in the manifest. Wayback occasionally serves a different capture than the one asked for.
- On a redirect, follow it within `web.archive.org` and record the snapshot actually served. If it leads off-site, log it and skip.

**Pacing, built for an archive that blocks:**

- **One request at a time, globally.** Not per site: archive.org sees one client, so two sites importing at once must share the budget. `_host_for_job` and `_busy_hosts` in `jobs.py` currently key on `Site.primary_host`. Both learn that a `wayback-import` job's host is `web.archive.org`, so per-host serialisation covers it.
- **Default 10 requests/minute** (a 6 s gap with jitter), configurable. There is no official published limit. Community reports put the CDX limit around 15/min, and 429s have become more frequent in 2026. The default stays under the lowest figure seen.
- **The outage page is never content.** In the same probe, a CDX query got `HTTP 503` with an HTML page titled "Internet Archive: Temporarily Offline", and an identical-shape query seconds later succeeded. Any non-2xx from `web.archive.org` itself is a retry. So is a response missing both `Memento-Datetime` and the `X-Archive-Orig-*` headers. Neither is ever written to a WARC or parsed as CDX rows (the CDX parser must reject a body starting `<html`).
- **429 or 503:** honour `Retry-After` when present. Otherwise back off exponentially from 60 s, capped at 1 h. Reuse the shape of the feed backoff in `services/feeds.py`.
- **Circuit breaker:** after 5 consecutive 429s the job pauses (status `partial`, resumable) and says so plainly. Hammering an IP-level block is how a temporary block becomes a long one.
- **Daily budget** (optional, e.g. 3,000 requests per day). When it is spent, the job pauses and the scheduler resumes it the next day. A 10k-URL import then runs politely over several days with nobody watching.
- **User-Agent** names Cairn and the contact URL from settings, so archive.org can identify the client.

**Resume:** a ledger at `<capture>/wayback-ledger.jsonl` records `url, timestamp, state (pending|done|failed|gone), attempts`. A paused or crashed job resumes from the ledger as the same capture, matching how paused wget captures already resume ([roadmap: "a paused capture that resumes as itself"](12-roadmap.md)). Each WARC part is closed every N records, so a crash loses at most the open part.

**Output** (one capture, like the ArchiveBox importer in `services/archivebox.py`):

- `Capture(kind="import", engine_id="wayback-import")`, directory `captures/<now>-import-wayback/`.
- `warc/part-NNNNN.warc.gz` written with `warcio`:
  - a `warcinfo` record: source = Internet Archive Wayback Machine, survey time, strategy, Cairn version;
  - per URL, a `response` record. `WARC-Target-URI` is the original URL. `WARC-Date` is the **Wayback snapshot time**, so replay and the capture timeline show when the content really existed, not when Cairn copied it. `WARC-Source-URI` is the `web.archive.org/...id_/` URL, plus a `WARC-Creation-Date` of now.
- `warc/part.cdx`, so later live crawls can dedup against these records (`_dedup_cdx` only reads `part.cdx`).
- `manifest.json` with snapshot timestamps, digest mismatches, failures, and the request count spent.
- Then `replay.update_index`. pywb already serves every WARC under `captures/*/warc/`, so imported pages replay alongside crawled ones with no replay changes.

Provenance stays visible everywhere: the capture list shows a "Wayback" badge, and each URL's capture history shows which copy came from archive.org.

### 4. Live gap crawl

The live crawl uses **the site's own politeness settings** (`wait_s`, `rate_limit`, concurrency 1). These are kept separate from the Wayback pacing: the rate-limited origin sets the gap crawl's speed, and archive.org's limits set the import's.

The live crawl gets the `live` + `always_live` buckets, **without** recursion into the imported pages. Otherwise wget rediscovers and refetches everything the import just saved.

- Reuse the `only_extra_seeds` path (`jobs.py:1610`), which bulk URL import already uses for "archive exactly these pages": depth 0, with page requisites.
- **Link discovery replaces recursion.** Recursion is how wget normally finds pages. Here, discovery comes from sitemap and feeds plus **links extracted from the imported HTML**, a local parse with no network. In-scope links found that way, and not already covered, are added to the `live` bucket before the crawl starts.
- **Asset backfill:** images and CSS referenced by imported pages but missing from both Wayback and the index go into the `live` bucket. They usually sit on CDN hosts (`blogger.googleusercontent.com` and the like), not the origin, so they cost the origin nothing.

In live-first, this phase runs as an ordinary full crawl first. Its 404s and gone pages then feed phase 3.

---

## Data and settings

- **New settings** (global, since the budget is archive.org's, not a site's): `wayback.rate_per_min` (10), `wayback.daily_budget` (off), `wayback.max_consecutive_429` (5), `wayback.contact_url`.
- **Per import request:** strategy, freshness cutoff, asset classes to include, whether to run the gap crawl automatically afterwards.
- **No schema migration** for v1. `Capture.kind="import"` and the engine id already fit. The survey cache and ledger are files in the site's archive directory, consistent with "the archive directory is the source of truth" ([03](03-data-model-and-storage.md)).

## UI

Site page → **Import from Wayback**:

1. Survey runs, then a coverage card shows the numbers from phase 1.
2. The user picks a strategy (pre-selected by whether the last live discovery reached the origin), the freshness cutoff and assets, and sees the estimated Wayback requests and days at the current budget.
3. Start. Progress shows requests spent today, the budget left, and the next resume time when paused by the budget or the circuit breaker.

---

## Risks

| Risk | Mitigation |
|---|---|
| archive.org blocks the server's IP | Conservative default, global serialisation, circuit breaker, daily budget; the survey is cheap so the blocked state is learned early |
| Snapshot is an archived error page served with status 200 (soft 404, Blogger content-warning interstitial) | Flag pages whose `<title>`/body match the platform's error and interstitial signatures; keep them but mark them in the manifest |
| Mixed-date replay: page from 2021, CSS from 2024 | Accepted; the capture timeline shows each record's real date. Prefer, per asset, the snapshot nearest the page that references it (v2) |
| URL variant sprawl (`?m=1`, comment params, http/https) | Scope and reject patterns plus SURT normalisation applied at survey time |
| The HTTP client silently decompresses `id_` bodies, so the digest fails and the header lies about the body | Raw-stream reads only; a test asserts `1f 8b` bytes survive into the WARC; the conformance test replays an imported record through pywb |
| An archive.org outage page is saved as the site's content | Never write a response without `Memento-Datetime`/`X-Archive-Orig-*`; the CDX parser rejects HTML bodies |
| Terms of use | Personal archival of public pages at a polite rate; no bulk redistribution. Note it in the UI help text |

## Testing

- Recorded CDX fixtures (paged and resume-keyed), plus a fake Wayback server in tests that can be told to 429, send `Retry-After`, serve a wrong digest, or redirect.
- Engine conformance harness (`engines/conformance.py`) against the fake server.
- Ledger resume: kill mid-run, restart, assert no URL is fetched twice.
- End-to-end: import, then `replay.update_index`, then pywb serves the page with the Wayback timestamp. Then an incremental live crawl writes revisits against the imported `part.cdx`.
- `warcio check` on every produced WARC.

## Milestones

1. **Survey + coverage report.** Read-only and useful alone: answers "how much does Wayback have?" in a few requests.
2. **`wayback-import` engine.** Fetch, WARC, `part.cdx`, index, pacing, ledger, global serialisation.
3. **Planner + gap crawl.** Buckets, link extraction from imported HTML, `only_extra_seeds` hand-off, asset backfill.
4. **Polish.** Daily budget with scheduled resume, live-first's "gone pages" feed into phase 3, nearest-date asset selection.

## Target site notes: fictionmania.tv

- **Soft 429 on the origin.** Blocked requests redirect to `/err429.html` and are served with 200 after roughly 47 requests in 84 s, and the block persists while requests continue (measured 2026-10-30, docs/18). The live gap crawl must recognise that page as a rate-limit signal and stop, not archive it. Otherwise phase 4 fills the archive with error pages exactly as the existing full captures did.
- **The same page may be in Wayback.** archive.org may have captured `err429.html` content under story URLs too. The import should treat any `id_` body matching the error page's size or digest (1,170 bytes here) as a missing snapshot and fall back to an older timestamp.
- **Wayback listing size:** `showNumPages` reports 98 CDX pages for the `www.fictionmania.tv/` prefix. A one-row-per-URL `collapse=urlkey` query over the whole prefix timed out (504), so the survey must page and reduce locally, as phase 1 already specifies.
- **archive.org was also throttling** on 2026-10-30: the availability API answered 429 on the first request of the session.

### Coverage survey (2026-11-01)

All 98 CDX pages were pulled one request at a time, about 10 s apart. Over roughly two sessions this produced about 20 transient 503/504s and no hard 429 block. Rows were then reduced locally.

| Measure | Result |
| --- | --- |
| Snapshot rows, whole host | 594,759 |
| Snapshot rows, story pages | 263,813 (200: 182k, 302: 42k, revisit `-`: 29k, 301: 10k, 404: 465) |
| Distinct storyIDs seen | 57,851 |
| storyIDs with a good text view | **57,298** (57,562 counting `details.html`) |
| Good copies by view | `readtextstory` 50,299 · `details` 42,679 · `readxstory` 32,661 · `readhtmlstory` 9,653 · `readststory` 2,895 |
| Error/boilerplate pages under story URLs | 86 rows: err429 digests, plus two digests shared by 33–48 storyIDs |

- **Story views.** A story appears under five URL shapes: `/stories/{readtextstory,readststory,readxstory,readhtmlstory,details}.html?storyID=N`. The import plan should pick one text view per story, preferring `readtextstory`, then `readxstory`/`readhtmlstory`, plus `details`. Fetching every view would roughly double the request budget for little gain.
- **storyIDs are opaque.** They are 13–20 digit numbers, not a sequence, so the site's total story count cannot be inferred from them. The "missing" share needs one count from the live site, such as a listing page that shows a total.
- **Freshness.** Newest good text copy per story, by year: 2017: 4,973; 2019: 27,669; 2020: 1,903; 2021: 4,577; 2022: 3,033; 2023: 2,623; 2024: 2,181; 2025: 5,325; 2026: 5,008. Wayback has picked up about 2,000–4,600 new stories every year since 2019, so the gap at the newest end should be small. Stories edited after their newest snapshot will import as the older text.
- **Contamination is low.** The err429 page is in Wayback (5 distinct digests on `/err429.html` itself), but only about 85 story snapshots carry it or other shared boilerplate. Digest matching plus the "digest shared by ≥20 storyIDs" heuristic catches them, and the import falls back to an older timestamp.
- **Effort estimate.** About 57,300 story fetches at the 10/min default is roughly 4 days from Wayback. The live origin blocks after about 47 requests and stays blocked for about 90 minutes, so the same set would take around 2.5 months. Existing full captures held only about 385 real pages each.
- **Survey tooling.** The scratch scripts used are a resumable CDX pager and a per-story reducer. They are the prototype for milestone 1. The pager's behaviour (one request at a time, `Retry-After`-aware backoff 60→1800 s, reject HTML bodies, save each page as it arrives) worked unchanged across a session restart.

## Open questions

1. ~~`id_` body encoding~~: answered by the 2026-10-30 probe (see phase 3). Still worth one more probe on an uncompressed original (an image) and a `br`-encoded one before milestone 2.
2. ~~Is the target site still online?~~ Yes, and it's the origin that rate-limits. So wayback-first is the primary strategy.
3. **Archive.org pacing at volume.** The CDX survey (98 requests at ~6/min, 2026-11-01) saw only transient 503/504s, but `id_` snapshot fetches are a different endpoint and remain unmeasured at volume, so the 10/min default is still a guess. Raise the default only once a few hundred fetches have run without a 429.
4. **Old versions.** Should the import bring only the newest snapshot per URL, or optionally several (e.g. one per year)? The WARC/CDXJ model handles several fine; only the request budget argues against it.
5. **Authenticated or archive.org-account access.** Logged-in users may get higher limits. Out of scope unless rate limits make the feature unusable.
