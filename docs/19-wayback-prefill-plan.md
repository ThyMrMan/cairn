# 19 — Wayback Pre-fill: implementation plan

Status: **Stages 1 and 2 built (2026-11-01, branch `wayback-prefill`); Stages 3–4 not started.** This turns the design in [17](17-wayback-prefill.md) into an ordered set of changes, each one PR. It builds on the audit's prerequisite list ([18](18-audit-2026-10.md#prerequisites-for-the-wayback-pre-fill)) and the fictionmania survey (17, "Coverage survey").

The first real user is **www.fictionmania.tv**. Wayback holds about 57,300 of its stories. The origin returns a disguised rate-limit page after about 47 requests. Every step below is judged by one question: does it get that site archived without fighting the origin?

```
Stage 0  Prerequisites      ──  5 small PRs, no user-visible feature
Stage 1  Survey             ──  "how much does Wayback have?" in the UI
Stage 2  Import engine      ──  the 57k stories land in a capture and replay
Stage 3  Plan + gap crawl   ──  only what Wayback lacks comes from the origin
Stage 4  Polish             ──  budgets, scheduled resume, live-first
```

Stages 1 and 2 already deliver most of the value for fictionmania. Stage 3 is what makes the feature general.

---

## Stage 0 — Prerequisites

Each one is small and independently useful. Order matters only where noted.

| # | Change | Where | Why the import needs it | Effort |
|---|---|---|---|---|
| 0.1 | **A resumed capture keeps its earlier WARCs** (audit #13). `_finalize` merges `collector.artifacts` with the capture's existing `warc_files` (or rescans `out/warc`). | `services/jobs.py` `_finalize` (~line 2063) | A 57k-URL import pauses and resumes many times. Without this, every resume drops the earlier parts from checksums, manifest and integrity. | S |
| 0.2 | **Jobs declare the host they load.** `_host_for_job` asks the engine first: a new optional engine capability `network_host` (e.g. `web.archive.org`) overrides `Site.primary_host`. `_busy_hosts` is rewritten to call `_host_for_job` per running job instead of joining on `Site.primary_host`. | `services/jobs.py` `_host_for_job`, `_busy_hosts`, `NETWORKED_JOB_TYPES`; `engines/protocol.py` capability docs | Two sites importing at once must queue on archive.org. An import must also **not** block a live crawl of its own origin, and the reverse. | S |
| 0.3 | **A gap crawl can dedup against imported records.** `_dedup_cdx` keeps returning `None` for a plain full capture, but honours a request flag `dedup: true` that a gap crawl sets. | `services/jobs.py` `_dedup_cdx`, `CAPTURE_REQUEST_KEYS` | Without it, the gap crawl re-stores the theme and shared assets the import already saved. | S |
| 0.4 | **One polite HTTP client** (audit #5): `cairn/net/polite.py`. One request at a time per host. Honours `Retry-After`, otherwise exponential backoff 60 s → 1 h. Stops after N consecutive 429/503 (raises `RateLimited`). Rejects HTML bodies where data is expected. Optional **raw stream** (no decompression), and an optional **soft-429 rule**: a redirect to, or body matching, a configured pattern counts as a 429. | new module; tests with a fake server | The survey and the importer both need it. It replaces the scratch `wayback_survey.py` loop, whose behaviour is proven: 98 CDX pages across a restart, about 20 transient 503/504s, no hard block. Discovery and feeds can adopt it later (audit #5, #21). | M |
| 0.5 | **Soft-429 handling for wget crawls.** A per-site scope field `rate_limit_page` (regex, e.g. `/err429\.html`). (a) The supervisor watches the wget log for a redirect to it and, after K hits, **pauses** the capture with a cool-down, so it resumes on its own later. (b) Postprocess drops records whose target or redirect matches the pattern from the index, so the error page never replays as a story. (c) A one-off maintenance command withholds the existing err429 records in fictionmania's old captures. | `engines/wget.py` log parsing, the supervisor in `services/jobs.py`, `services/postprocess.py`, `services/skiplist.py`, `cli.py` | Stage 3's gap crawl hits this origin. Without it, the gap crawl repeats the old captures' result: 87% error pages. | M |

0.1–0.3 are independent. 0.4 comes before Stage 1. 0.5 must land before Stage 3 but doesn't block Stages 1–2.

---

## Stage 1 — Survey and coverage report

**Goal:** from a site's page, one click shows how much of the site Wayback holds, without touching the origin.

**Backend**

- `cairn/wayback/cdx.py`: a CDX pager on top of `polite.py`.
  - Use `showNumPages` + `page=N`. This was proven on fictionmania: 98 pages, about 6,000 rows each. 17 suggested `showResumeKey`; keep that only as a fallback if a page errors repeatedly.
  - Fields: `urlkey,timestamp,original,statuscode,mimetype,digest,length`.
  - Save each page gzipped under `<site>/import/cdx/page-NNN.txt.gz` as it arrives, so a restart resumes. The cost is about 120 MB uncompressed for fictionmania, about 13 MB gzipped.
- `cairn/wayback/survey.py`: stream the pages and reduce locally.
  - **Scope filter.** Apply hosts, `path_prefix`, `reject_patterns` and the skip list. Normalise with SURT.
  - **Newest good snapshot per URL.** Good means 2xx, not a revisit-only row, and not contaminated.
  - **Contamination filter.** Exclude digests of the site's `rate_limit_page` (its own CDX rows reveal them: 5 digests on fictionmania). Also exclude any digest shared by at least 20 URLs of the same group (two such digests on fictionmania, covering 33–48 stories each).
  - **URL groups** (new, per-site, optional). This is a scope field `url_groups: [{name, pattern, key, prefer: [...]}]`. For fictionmania it maps the five `/stories/*.html?storyID=N` views onto one item keyed by `storyID`, preferring `readtextstory` → `readxstory` → `readhtmlstory` → `readststory`, plus `details` as a companion. The report counts **items**, not URLs, and the import fetches one view per item. That halves the request budget.
  - Write `<site>/import/wayback-survey.json`: the counts from 17's phase 1, the per-year freshness histogram, counts per group, and the query time.
- **New job type `wayback-survey`.** Fictionmania's pull took about 20–40 minutes, so it can't run inside an API request.
  - Add it to `JOB_TYPES` (`db/models.py`) and `NETWORKED_JOB_TYPES`, with host `web.archive.org` via 0.2.
  - Check whether `JOB_TYPES` is enforced in the database; if it is, add an alembic migration.
  - Progress events: `page k of N`.
- **API.** `POST /sites/{id}/wayback/survey` starts it. `GET /sites/{id}/wayback/survey` returns the cached JSON. Both get the `Csrf` dependency.

**Frontend**

- On the site page, add a **Wayback** card showing coverage numbers, freshness by year, the top URL groups, contamination found, and the estimated import time at the configured rate. It also has a "Refresh survey" button.
- The card needs an optional "how many items does the live site have?" number. Survey data can't give the total, because storyIDs are opaque, so the user types it in and gets a coverage percentage.

**Tests:** recorded CDX fixtures (including a 503 outage HTML page and a short last page); scope and SURT reduction; the contamination heuristic; URL-group collapse; resuming after a kill.

**Done when** the fictionmania survey runs from the UI and reproduces the numbers in 17: about 57,300 items, with the same freshness histogram.

Effort: M–L.

### Stage 1 as built

- **Code.**
  - `cairn/net/polite.py`: the polite client (0.4).
  - `cairn/wayback/cdx.py` and `cairn/wayback/survey.py`: the listing pull and the local reduction.
  - `services/wayback.py`: per-site options and the paths the survey writes.
  - `api/routers/wayback.py`: the three endpoints, listed in [09](09-api.md#wayback-pre-fill).
  - `frontend/src/components/Wayback.tsx`: the card on the site page.
- **Settings live in `sites.scope_settings`**, not in the scope tables: `url_groups`, `rate_limit_page`, and `wayback.live_items`.
- **0.2 is half done.** Job types can name a fixed host (`FIXED_HOST_JOB_TYPES`), and `_busy_hosts` now asks `_host_for_job` per job. The engine capability `network_host` comes with Stage 2, when an engine first needs it.
- **Shutdown stops a survey.** The survey runs on a worker thread that can sit in an archive.org backoff for up to an hour. It checks the supervisor's stopping flag as well as cancel, so a restart doesn't wait for it.
- **Added beyond the plan: a size estimate** (2026-11-02). The report sums the CDX `length` of every selected snapshot, which is archive.org's compressed WARC record size. The total is split into pages and assets, with a count of rows that list no length; each section in the breakdown carries its share too. It leans high, because copies archived uncompressed are counted at full size but arrive and are stored gzipped. The October fictionmania pull did not request `length`, so its size is unknown until the survey runs in the app.
- **Added beyond the plan: "Biggest parts of the import".** This breakdown by first path segment is what showed the noise in fictionmania's import (below).

**Run against the real fictionmania listing** (the 98 pages from 17, reduced in about 6 s). The settings were the stories group, `/err429\.html` as the rate-limit page, and `/review/` rejected:

| Measure | Result |
|---|---|
| Story items | **57,562** (matches 17), 99,977 fetches with `details` kept, 38,293 variant URLs skipped |
| Whole import | 257,729 fetches (166,718 pages, 91,011 assets), about 18 days at 10/min |
| Error pages skipped | 353 URLs, from 5 rate-limit digests and 8 shared digests |

The breakdown shows what else is in that 258k:

| Section | Fetches | What it is |
|---|---|---|
| `/storyimages/` | 43,355 | story illustrations (assets) |
| `/stories/*.txt` | 37,762 | plain-text story files, possibly the original uploads |
| `/searchdisplay/` + `/search/` | 37,342 | listing and search-result pages |
| `/stories/report.html` | 25,066 | the "report this story" form, one per story |
| `/cdn-cgi/` | 5,980 | Cloudflare scripts |

Rejecting `report.html`, the listings and `/cdn-cgi/` would bring the import to about 189k fetches, about 13 days at 10/min. That choice belongs in the site's scope, not in code.

---

## Stage 2 — `wayback-import` engine

**Goal:** turn the survey's selected snapshots into a normal capture that replays, is searchable, and is badged "Wayback".

**Engine** (`engines/builtin/wayback-import/engine.yaml` + `cairn/engines/wayback.py`, same subprocess contract as wget)

- Capabilities: `outputs: [warc, cdx, log]`, `incremental: false`, `resumable: true`, `network_host: web.archive.org`, `max_concurrency: 1`.
- Config: `rate_per_min` (10), `max_consecutive_429` (5), `daily_budget` (off), `contact_url`.
- **Seeds** are `timestamp url digest` lines from the plan file. Order: HTML items first, then CSS, then images.
- **Fetch** `web/<ts>id_/<url>` with the polite client in raw-stream mode, and follow redirects only within `web.archive.org`.
  - A response with neither `Memento-Datetime` nor `X-Archive-Orig-*` is an outage page: retry it, never store it.
  - A body matching the site's rate-limit page means: mark the URL, then try the next-older good snapshot from the survey.
- **WARC.** Use `warcio`. Write a `warcinfo` record, then one `response` record per URL:
  - the HTTP header block is rebuilt per 17 phase 3;
  - `WARC-Date` is the snapshot time;
  - `WARC-Source-URI` is the `id_` URL;
  - the SHA-1 is checked against the CDX digest, and a mismatch is flagged, not dropped.
  - Roll to a new `part-NNNNN.warc.gz` every 1,000 records.
- **`part.cdx`** is written in the same 11-field format wget produces (`CDX_HEADER` in `jobs.py`), so later crawls can dedup against it.
- **Ledger.** `wayback-ledger.jsonl` in the capture directory doubles as the engine's resume state, so pause, resume and crash all continue from it. A spent daily budget or a tripped breaker ends the run `partial` with a `resume_after` hint.

**Core**

- `_prepare` gets a `kind == "wayback"` branch. It builds seeds from `<site>/import/wayback-plan.jsonl` (Stage 3); until Stage 3 exists, that means every good item from the survey. The capture is `Capture(kind="import", engine_id="wayback-import")` in `captures/<now>-import-wayback/`.
- Postprocess runs unchanged, giving index, search text and thumbnails. `replay.update_index` already picks up `captures/*/warc/`.
- Scheduler: a `partial` import carrying `resume_after` is re-queued at that time. A minimal version of this goes in Stage 2, because a 4-day import otherwise needs a click every few hours.

**Frontend:** a "Start import" button on the Wayback card. Progress shows requests spent, items done out of total, and the next resume time when paused. The capture list shows a "Wayback" badge.

**Tests:**
- Fake Wayback server: 429 with and without `Retry-After`, an outage page, a wrong digest, a redirect, an err429 body.
- The engine conformance harness.
- gzip bytes (`1f 8b`) survive into the WARC.
- Kill-and-resume fetches nothing twice.
- `warcio check` passes.
- pywb serves an imported record with the snapshot date.

**Done when** a 500-story fictionmania import replays correctly. Then the full import runs unattended for about 4 days at 10/min.

Effort: L.

### Stage 2 as built

- **Engine.** `cairn/engines/wayback.py` is registered as the built-in `wayback-import` engine (`engines/builtin/wayback-import/engine.yaml`).
  - It declares `site_engine: false`. The engine picker hides it, and the sites API refuses it as a site's crawler.
  - It reads the selection the capture copied from the survey (`wayback-selection.jsonl.gz` in the capture directory, so a resume keeps the original list).
  - It fetches `id_` snapshots raw, without following redirects. A response with `Memento-Datetime` or `X-Archive-Orig-*` headers is stored as archived, including archived redirects. A redirect without those headers is Wayback moving to a nearby timestamp, and is followed (up to 3 hops, on archive.org only). Anything else from archive.org is never stored.
  - A 404, or a body whose digest is a known error page, falls back to the next older snapshot.
- **Output.** Records go into `warc/wayback-<stamp>-NNNNN.warc.gz`, 1,000 per part, and a part name is never reused. `WARC-Date` is the snapshot time, and `WARC-Source-URI` is the `id_` URL. Original headers come back from `X-Archive-Orig-*`, with `Content-Length` recomputed. `warc/part.cdx` uses wget's 11-field layout, so later crawls can dedup against it.
- **Resume.**
  - `wayback-ledger.jsonl` records each URL as done, gone or failed. A URL that fails 3 times is left alone.
  - The engine pauses itself, reporting `self_paused` and `resume_after_s`, in three cases: after 5 consecutive 429s (cool-down 60 min), when the optional daily budget (`wayback.daily_budget`) is spent, and at the capture time cap. The time cap ends a segment, not the import: the work so far is indexed, and the import resumes a minute later.
  - `_finalize` records `capture.request.resume_at`. `Scheduler._due_resumes` queues the resume when that time comes and nothing else is running for the site.
- **Core.**
  - `_prepare` has a `kind == "wayback"` branch: the importer engine, no page cap, no site politeness, and **no access-profile cookies**, since every request goes to archive.org.
  - `_host_for_job` maps capture kind `wayback`, including its resumes, to `web.archive.org` (`FIXED_HOST_CAPTURE_KINDS`). This is simpler than the engine capability the plan proposed, and does the same job.
  - **0.1 done:** a resumed capture keeps its earlier WARCs and URL counts (`merge_artifacts`, `_Prepared.resumed_from`).
- **API and UI.** `POST /api/sites/{id}/wayback/import` starts an import or continues the paused one. The Wayback card has an Import section showing status and the automatic resume time, and import captures carry a "Wayback" badge.
- **Tests** (`tests/test_wayback_import.py`, 15):
  - A fake archive.org covers raw gzip preservation, the snapshot date, archived and Wayback redirects, older-snapshot fallback, outage pages, ledger resume, the 429 pause, the daily budget and the time-cap segment.
  - One end-to-end run puts the real engine process through the queue against a localhost fake (pointed at by `CAIRN_WAYBACK_BASE`, read from the environment only) and checks the replay index.

**Live check (2026-11-01), 5 real fictionmania snapshots** (3 story texts, 2 details pages) taken from the survey selection:
- All 5 were imported: 5 requests in 25 s at 10/min, with real titles and the original 2017, 2024 and 2026 dates.
- **Finding 1: tracking variants were being chosen.** Wayback holds Facebook-shared variants such as `…storyID=N&mibextid=AFdixm`, and the survey was picking them because they were newer. Replay finds a page by its URL, so it would have missed those stories. The survey now prefers the address with the fewest query parameters, then the newest copy. On the real listing, 0 of the 99,977 story picks carry extra parameters.
- **Finding 2: copies archived uncompressed are gzipped by archive.org on the way out.** Those 2017 copies match their CDX digest only after decompressing. The stored record is correct either way. The digest check now accepts both forms.

**Still to do:** a 500-story sample from the deployed instance before the full import.

---

## Stage 3 — Planner and gap crawl

**Goal:** fetch only what Wayback lacks, from the origin, at the origin's pace.

- `cairn/wayback/plan.py` (local, no network) sorts every item into `skip / wayback / live / always_live`, as described in 17 phase 2.
  - Inputs: the survey, the site's own index, the discovered-URL sidecar (`discovered-urls.txt.gz`, from the quick wins), and a freshness cutoff.
  - Output: `<site>/import/wayback-plan.jsonl`, plus per-bucket counts shown on the card.
- **Link harvest from imported pages.** `services/htmlrefs.parse_page` runs over the imported WARCs. In-scope links not covered by any bucket go to `live`, and missing assets go to `live` too (usually CDN hosts, so they cost the origin nothing).
- **Gap crawl.** This is a capture with `only_extra_seeds` (the existing bulk-URL path) seeded from `live + always_live`, with `dedup: true` (0.3), the site's own `wait_s`, and soft-429 handling (0.5). The import's job finishes by offering it, or starts it if the user asked for "run gap crawl automatically".
- **Fictionmania gap.** The gap is new stories since each listing's last snapshot plus anything Wayback never had. This is where the user-entered total from Stage 1 turns into a number. The site's listing pages (`/searchdisplay/newN.html`, `datedisplay`) are the cheap way to enumerate new storyIDs: a few dozen origin requests, not thousands.

**Tests:** planner bucket rules against fixtures; a gap crawl over a fake origin that soft-429s after N requests pauses, resumes, and stores no error pages.

Effort: M–L.

---

## Stage 4 — Polish

- A global settings page section for `wayback.*`, the daily budget, and the contact URL.
- live-first strategy: the gone and 404 URLs of a live crawl feed an import.
- Nearest-date asset selection.
- Per-URL provenance in the capture-history view.
- Moving discovery and feed polling onto `polite.py` (audit #5, #21).
- Docs: update 05 (engines), 08 (scheduling) and 15 (feature tour). Mark 17 as built.

---

## Running it on fictionmania

1. Land 0.1–0.4, then Stage 1. Run the survey and check that it matches 17's numbers.
2. Land Stage 2. Import a 500-story sample, check replay, then start the full import. It takes about 4 days at 10/min, with the daily budget off and breaker defaults.
3. In parallel, land 0.5 and withhold the err429 pages from the existing captures.
4. Land Stage 3. Plan, then run the gap crawl at a slow `wait_s`. The safe origin rate is still unmeasured; start around 15 s and watch the soft-429 counter.

## Decisions (2026-11-01)

1. **URL groups are a generic per-site setting** (`scope.url_groups`), not a fictionmania preset.
2. **Review pages are skipped.** For fictionmania that is done with the site's reject patterns (`/review/`). The survey reports how many in-scope URLs it rejected, so the cost of a pattern is visible.
3. **The gap crawl waits for a click.** It never auto-starts after an import.
