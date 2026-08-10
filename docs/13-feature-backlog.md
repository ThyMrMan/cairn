# 13 — Feature Backlog

Ideas beyond the stated requirements, drawn from the tools in the original evaluation and from others worth knowing about. Ranked within each tier by value-to-effort.

Nothing here is required. The point is to have the good ideas written down before you're deep enough in the build to stop noticing them.

---

## Tier 1 — High value, clearly worth building

### Full-text search across all archives

SQLite FTS5 over text extracted from captured HTML (`trafilatura` or `readability-lxml` to strip navigation and boilerplate), with results linking straight into replay at the right capture.

**Why it's the top item.** With hundreds of archived sites, *"which of my archives mentioned this?"* becomes the primary way you use the tool. Every archive project reaches the point where the archive is too large to browse and search is the only interface that scales. Karakeep's full-text search is its most-praised feature for exactly this reason.

**Effort:** medium. Text extraction is a post-processor; FTS5 is built into SQLite; the UI is one search page. Budget ~10–15% additional storage for the text index.

### WACZ export + shareable replay

Package a site as a single `.wacz` and hand it to anyone — it opens in [ReplayWeb.page](https://replayweb.page/) with no server. Also the best offsite backup format, since it's self-describing and tool-independent.

**Effort:** low. `wacz create` over existing WARCs, plus a download button.

### Archive integrity verification

Weekly job re-checksumming every WARC against `manifest.json`, reporting mismatches and missing files.

**Why it matters more than it sounds.** Archives are cold data nobody reads for years. Bit rot on a NAS array is real, and without verification you find out during a restore. This is the difference between noticing in a week and noticing never.

**Effort:** low. A scheduled job and a status page. Pair with an "archive health" dashboard: total size, growth trend, oldest unverified capture, last successful backup.

### Capture diffing

Show what changed between two captures of a page — rendered text diff plus a list of added/removed/changed resources.

**Why.** It's the answer to "should I keep running full recaptures?" and it turns the archive into a record of change rather than just a copy. `changedetection.io` built a following on this alone.

**Effort:** medium. Both versions are already in the index; the work is UI.

### `yt-dlp` media capture

Post-processor scanning captured HTML for YouTube/Vimeo/etc. embeds, with per-site opt-in to download the media into `derived/media/`.

**Why.** Neither wget nor a browser crawler captures video streams. An archived post with a dead embed is a common and permanent disappointment — and it's the gap you discover years later when the video is gone.

**Effort:** low. Opt-in per site, with a clear storage warning.

### Notification integration

ntfy, Apprise, and generic webhooks. Detailed in [08](08-feeds-and-scheduling.md#notifications).

**Effort:** low. Apprise alone covers 80+ services.

---

## Tier 2 — Strong ideas, more work

### Retention policies

Per-site rules: keep N full captures, keep one per month beyond that, never prune a capture containing URLs that no longer exist upstream, never prune the first capture.

**Why the exception clauses matter.** Naive retention deletes exactly the captures that justify the archive — the ones holding content that's gone from the live web. Any retention feature must be able to identify and protect those, or it's a liability.

**Effort:** medium. Requires cross-capture URL analysis and a very careful dry-run mode.

### Import from ArchiveBox

Read an existing ArchiveBox `index.sqlite3` + `archive/` directory, group snapshots by domain into sites, index their WARCs into a collection, carry tags across.

**Why.** You already have an ArchiveBox instance with work in it, and per the original notes it's already producing per-page WARCs that just need indexing — which is exactly what this tool does. Also removes the main adoption barrier for anyone else in the same position.

**Effort:** medium. Mostly a schema-mapping exercise; ArchiveBox's `index.sqlite3` layout has shifted across versions, so target recent ones and fail clearly on older.

### Browser-based discovery

Once Chromium is in the image (M5), run discovery through a real browser to catch hosts referenced only from JavaScript and content behind infinite scroll.

**Effort:** medium. The discovery engine gains a browser variant; classification logic is unchanged.

### Public share links

Signed, expiring, optionally password-protected links to a single archived page or a whole site, replayed read-only.

**Why.** Linkwarden's sharing is a genuinely popular feature and this is the natural request once you've archived something worth showing someone.

**Effort:** high, and it's the feature most likely to introduce a security hole — it deliberately punches a hole in the auth boundary, on the origin that replays untrusted JavaScript ([11](11-security.md)). If built: separate origin, no session cookies, tokens scoped to one collection, rate limited, revocable, off by default.

### Scheduled report digest

Weekly email or notification: sites captured, new posts found, failures, storage growth, upcoming credential expiries, integrity results.

**Effort:** low-medium. High value for an unattended tool — it's how you notice something broke three weeks ago.

### Multi-seed sites

Some blogs span domains (a custom domain plus the blogspot original, or a site that migrated). Allow multiple seeds under one site with one scope, one index, and one replay collection.

**Effort:** medium. Mostly already supported by the data model; the work is UI and scope resolution.

---

## Tier 3 — Worth knowing about

### Personas beyond cookies

ArchiveBox's "personas" bundle cookies + user agent + browser profile. This design already has profiles carrying cookies and UA; extending to full browser profiles (localStorage, IndexedDB, service workers) makes them work with `browsertrix-crawler --profile` directly.

### Site health monitoring

Periodically check whether archived sites are still live. Surfacing *"3 archived sites are now returning 404"* is both interesting and a strong argument for the tool's existence.

### Bookmarklet / browser extension

One-click "archive this page" from the browser. Karakeep and Linkwarden both have one and it's the most-used feature in both.

### Bulk URL import

Paste or upload a list of URLs; group into sites by host automatically. Useful for migrations and for one-off collections.

### Archive annotations

Notes and highlights on archived pages. Turns an archive into a research tool. Substantial work (anchoring annotations to replayed content is genuinely hard) but it's what people actually want from a personal archive.

### Storage tiering

Move captures older than N months to a slower or cheaper tier (array-only, or an rclone remote), keeping the index local so replay still resolves. Fetch on demand.

### Prometheus metrics

`/metrics` with job counts, capture durations, error rates, storage. Easy, and Unraid users often already run Grafana.

### Read-only "reader" view

Extracted article text rendered cleanly, no CSS, no JS. Fast, accessible, and immune to broken replay — and it's what you actually want when reading rather than verifying.

### Federated/mirror sync

Sync archives to a second instance. Real 3-2-1 for archives that matter.

---

## Ideas taken from each evaluated tool

| Source | Idea worth stealing |
|---|---|
| **ArchiveBox** | Personas (cookies + UA + profile bundled); multiple output formats per page (WARC + screenshot + PDF + single-file + readable text) as user-selectable extractors |
| **Karakeep** | Full-text search as a primary interface; AI-assisted auto-tagging; one-click browser extension; "auto-hoard from RSS" as a first-class concept rather than a scheduler afterthought |
| **Linkwarden** | Collections + sub-collections (this design's folders); preservation *formats* as an explicit user choice; sharing with expiry |
| **Browsertrix** | Interactive login profiles via a remote browser session — the strongest answer to the bypass problem, and already planned as M5; behaviors (autoscroll, lazy-load triggering) as reusable named scripts; WACZ as the portable unit |
| **pywb** | Combined index across many WARCs (the core replay decision); Memento/timemap support; access-control lists for excluding URLs from replay |

## Tools not in the original evaluation, worth a look

| Tool | Why |
|---|---|
| [**wget2**](https://gitlab.com/gnuwget/wget2) | Multi-threaded, HTTP/2, WARC support. Near drop-in speed upgrade |
| [**grab-site**](https://github.com/ArchiveTeam/grab-site) | Archive Team's wpull-based crawler: curated ignore-sets, live dashboard, dynamic ignore editing mid-crawl. The ignore-sets in particular are years of accumulated knowledge about what's junk on the web — worth mining even if you don't use the tool |
| [**warcprox**](https://github.com/internetarchive/warcprox) | MITM recording proxy. Lets *any* client record to WARC, including your own browser. The universal escape hatch |
| [**monolith**](https://github.com/Y2Z/monolith) | Single-file HTML with everything inlined. Rust, fast, tiny |
| [**single-file-cli**](https://github.com/gildas-lormeau/single-file-cli) | Same idea, browser-based, handles JS-rendered pages |
| [**changedetection.io**](https://changedetection.io/) | Page-change watching. The mechanism is simple enough to build in rather than integrate |
| [**Zeno**](https://github.com/internetarchive/Zeno) | Internet Archive's newer Go crawler, WARC output, built for scale |
| [**cdxj-indexer**](https://github.com/webrecorder/cdxj-indexer) | Already in the design; also useful standalone for auditing what's in a WARC |
| [**py-wacz**](https://github.com/webrecorder/py-wacz) | WACZ packaging |
| [**trafilatura**](https://trafilatura.readthedocs.io/) | Best-in-class boilerplate removal for the text-extraction step |
| [**Apprise**](https://github.com/caronc/apprise) | One dependency, 80+ notification services |
| [**restic**](https://restic.net/) | Deduplicating encrypted backup — ideal for the WARC tree, since WARCs are immutable and dedup well |
| [**miniflux**](https://miniflux.app/) / [**FreshRSS**](https://freshrss.org/) | If you already run one, import subscriptions rather than re-adding feeds by hand |

---

## Anti-features

Things to deliberately not build, recorded so the question doesn't come up repeatedly:

**Multi-user with permissions.** The requirement is explicitly single-user. Roles, sharing, and per-user quotas would touch every endpoint and every query for zero benefit here.

**A hosted/cloud version.** Different product, different threat model, different everything.

**Automatic paywall or CAPTCHA circumvention.** Access profiles let *you* supply credentials you already have. Building bypasses is a different thing with different consequences.

**A general-purpose scraper.** This archives sites as they are. Structured data extraction, price monitoring, and content transformation are adjacent problems that would distort the design.

**Its own WARC replay implementation.** pywb exists and is good. This is a multi-year project that would consume everything else.

**Rewriting archives in place.** WARCs are immutable ([D2](00-decisions.md#d2--index-across-warcs-never-merge-or-concatenate-them)). Every "fix the archive" feature is really a "capture again" feature.
