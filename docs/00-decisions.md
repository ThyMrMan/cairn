# 00 — Technical Decisions

Every non-obvious choice, why it was made, and what was rejected. Read this before writing code; if you disagree with one, change it here first.

---

## D1 — One capture job per site, not per URL

**Decision.** A capture is a *crawl of a scoped site*, producing a set of WARC segments in one directory, indexed as one unit. Individual URLs are rows in a table pointing into that capture.

**Why.** The ArchiveBox evaluation's issues 5 and 6 are the same root cause: one Snapshot per URL means hundreds of directories per blog and no way to browse the site as a cohesive whole. Making the *site* the unit of work fixes organization, replay, storage overhead, and mental model simultaneously.

**Rejected.** Per-URL snapshots with a grouping layer on top. It preserves the flaw and adds indirection.

---

## D2 — Index across WARCs; never merge or concatenate them

**Decision.** Each capture writes segmented WARCs (`part-00000.warc.gz`, …). A CDXJ index maps every URL to `(file, offset, length)`. Replay reads the index. Files are never rewritten, merged, or reorganized after they're written.

**Why.** This is how the Wayback Machine works and it's what the original notes concluded (issue 6). WARC files become immutable once closed, which makes checksumming, backup, dedup, and incremental capture all straightforward. Concatenating `.warc.gz` members is technically valid but saves nothing — you still have to index wherever the bytes land.

**Implication.** Index rebuild is always cheap and safe to re-run. Treat CDXJ as derived data that can be deleted and regenerated.

---

## D3 — wget for v1, behind an engine interface from day one

**Decision.** Ship exactly one engine (`wget-warc`) in M1, but define and implement the engine contract ([05](05-capture-engines.md)) at the same time. Core never calls wget directly.

**Why.** wget is boring, well-understood, present in every base image, has first-class WARC support including `--warc-dedup`, and needs no browser. It's also fundamentally limited (no JS, no lazy-load, no infinite scroll), so a second engine is inevitable. Building the seam late means retrofitting it through the job runner, the UI, and the schema.

**Cost.** The first engine is slightly more work than shelling out. Accept it.

---

## D4 — Tampermonkey userscripts run in a *pre-flight*, not during the crawl

**Decision.** A userscript never runs "inside" a wget crawl — that's impossible. Instead, an access profile in `userscript` mode runs a short headless-Chromium pre-flight: load the seed URL with the script injected at `document-start`, let it dismiss the interstitial, export the resulting cookie jar to Netscape format, then run wget with `--load-cookies` pointing at it. The minted jar is cached and re-minted when it expires or the crawl gets an interstitial response.

**Why.** It satisfies "import either cookies or Tampermonkey JS, selectable per site" with one downstream mechanism. Both modes produce a cookie jar; the engine only ever sees a cookie jar. Adding an interactive login mode later ([06](06-access-profiles.md)) slots into the same abstraction as a third producer.

**Rejected.** Making userscript support conditional on a browser engine. That would mean "pick cookies *or* userscript" also silently means "pick wget *or* browser", coupling two unrelated choices in the UI.

---

## D5 — SQLite, not Postgres

**Decision.** SQLite in WAL mode, one file, migrations via Alembic. No external database container.

**Why.** Single user, single writer, low write volume, and the whole value proposition is "one container on Unraid." A Postgres dependency doubles the deployment surface for zero benefit at this scale.

**Constraint.** The DB file must live on the cache pool, not the array — see [10](10-deployment-unraid.md#the-sqlite-on-fuse-footgun). Enforce `WAL`, `busy_timeout=5000`, `synchronous=NORMAL`, `foreign_keys=ON` on every connection.

**Revisit if.** Multi-user or multi-node ever becomes a requirement. The schema is portable; the ORM layer should not use SQLite-specific SQL outside migrations.

---

## D6 — In-process job runner, not Celery/Redis

**Decision.** Jobs live in a SQLite `jobs` table. An asyncio supervisor in the API process claims jobs, spawns engine subprocesses, streams their NDJSON events to the DB and to SSE subscribers, and enforces a global concurrency cap.

**Why.** Same reasoning as D5. Capture jobs are long-running subprocesses, not CPU-bound Python — asyncio supervises them fine. Redis + a broker + a worker container is three more moving parts to explain in an Unraid template.

**Constraint.** Jobs must survive restart: on boot, mark `running` jobs as `interrupted` and offer resume. Engines must be idempotent enough to re-run.

---

## D7 — Replay via pywb, on a separate origin

**Decision.** pywb runs as a second process in the same image on its own port (default `8081`), configured with one collection per site. The UI embeds it in an iframe.

**Why.** pywb is the mature WARC replay server, handles framed replay, URL rewriting, and multi-WARC collections natively. Writing a replay layer is a multi-year project.

**Security requirement, not optional.** Replayed pages execute archived JavaScript. If replay shares an origin with the app, an archived page can read the session cookie and drive the API. Replay therefore gets a distinct origin (separate port, ideally separate hostname), app cookies are never scoped to it, and pywb runs with CSP enabled. Detail in [11](11-security.md#replay-is-untrusted-code-execution).

---

## D8 — Folders are the primary hierarchy; tags are cross-cutting

**Decision.** Every site lives in exactly one folder (a materialized-path tree). Sites carry zero or more tags. The on-disk archive tree mirrors the folder tree. A separate generated `by-tag/` symlink tree mirrors tags.

**Why.** ArchiveBox issue 7 — tags were DB-only and the archive directory was a flat pile of timestamps. Folders map cleanly to a filesystem; tags don't (a site has many tags, a file has one path), so tags get symlinks instead of being forced into the primary layout.

**Consequence.** Moving a site between folders moves files on disk. That's a real operation with a real cost (rename within the same mount is instant; across mounts it's a copy). Make it a job, not a synchronous request.

---

## D9 — WARC is the source of truth; mirrored files are optional

**Decision.** Captures default to `--delete-after` — wget downloads, writes the WARC, and discards the on-disk mirror. A per-site toggle keeps the mirror for people who want plain files.

**Why.** Keeping both doubles storage for no archival benefit. Everything derived (text for search, screenshots, exports) can be regenerated from the WARC.

---

## D10 — Discovery is a separate, cheap, re-runnable job

**Decision.** "Index the site" is its own job type with its own results table. It never writes WARCs. It can be re-run at any time and its results are diffed against the previous run.

**Why.** The domain-selection UI needs data before any capture happens, discovery is fast (seconds to a minute) versus a capture (hours), and re-running it on an established site is how you notice the blog started embedding a new CDN.

---

## D11 — CDXJ for replay, wget's CDX only for dedup

**Decision.** wget's `--warc-cdx` output is kept solely to feed the next capture's `--warc-dedup`. The index pywb reads is CDXJ, generated separately by `cdxj-indexer` over the WARC files.

**Why.** They're different formats for different consumers and conflating them causes subtle replay bugs. Generating CDXJ from the WARCs also means the index is verifiable against the actual bytes rather than trusting the crawler's self-report.

---

## D12 — Python backend

**Decision.** Python 3.12 + FastAPI.

**Why.** The entire WARC ecosystem is Python: `warcio`, `cdxj-indexer`, `pywb`, `py-wacz`, `warcprox`, plus `feedparser`, `tldextract` (Public Suffix List), `selectolax`/`lxml`, and Playwright bindings. Writing this in Go or Node means shelling out to Python anyway or reimplementing parsers.

**Rejected.** Go (fast, single binary, but no WARC ecosystem), Node (SingleFile and browsertrix are Node, but replay and indexing are not).

---

## D13 — React SPA, not server-rendered

**Decision.** React + Vite + TypeScript, built to static files, served by FastAPI at `/`.

**Why.** The UI has genuinely stateful surfaces: a folder tree with drag-and-drop, a domain-selection table with bulk operations, live-streaming job logs, and an embedded replay frame. Server-rendered HTML + HTMX would work and would be less code, but the domain picker and log viewer would both fight it.

**If you want less work.** HTMX + Jinja2 + SSE is a legitimate downgrade path that removes the Node build entirely. Decide before M2; switching after the domain picker exists is a rewrite.

---

## D14 — Single Docker image, multiple processes

**Decision.** One image, s6-overlay supervising the API, the job runner (in-process, so really just the API), and pywb. Two exposed ports.

**Why.** Unraid users install single containers from Community Applications. A `docker-compose` requirement is a meaningful adoption barrier, and "why do I need three containers" is the first question anyone asks.

**Rejected.** Separate `app` + `pywb` + `worker` containers. Offered as an *optional* compose file for people who prefer it, not the default.

---

## D15 — Secrets encrypted at rest, write-only in the API

**Decision.** Cookie jars, userscripts, and any credentials are encrypted with AES-GCM using a key derived from `CAIRN_SECRET_KEY`. The API accepts them on write and never returns them on read — `GET` returns metadata only (hostname coverage, expiry, fingerprint).

**Why.** A cookie jar for a logged-in session is a credential. This instance is internet-exposed by design. If the UI can display it, an XSS or a stolen session hands it over.

---

## Decision summary

| # | Decision | Primary driver |
|---|---|---|
| D1 | Site-scoped captures | ArchiveBox issues 5, 6 |
| D2 | Index, never merge | Wayback model; immutability |
| D3 | wget first, engine seam from day one | Boring default + known ceiling |
| D4 | Userscripts mint cookies in a pre-flight | wget has no JS; unify both auth modes |
| D5 | SQLite | Single-user, single-container |
| D6 | In-process job runner | Same |
| D7 | pywb on a separate origin | Maturity + replay is untrusted code |
| D8 | Folders primary, tags via symlinks | ArchiveBox issue 7 |
| D9 | WARC is truth, mirror optional | Storage |
| D10 | Discovery is its own job | UI needs data before capture |
| D11 | CDXJ for replay, CDX for dedup | Format correctness |
| D12 | Python | WARC ecosystem is Python |
| D13 | React SPA | Stateful UI surfaces |
| D14 | One image | Unraid distribution |
| D15 | Secrets encrypted, write-only | Internet-exposed by design |
