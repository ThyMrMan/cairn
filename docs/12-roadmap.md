# 12 — Roadmap

Nine milestones. Each ends with something demonstrably working — no milestone is "infrastructure only." Sequenced so the riskiest unknowns get validated early and the enjoyable parts arrive soon enough to sustain momentum.

**Before M0, spend an afternoon on the spike below.** It de-risks the single assumption everything else rests on.

---

## M-1 — Validation spike (½ day, before committing to anything)

Not a milestone; a go/no-go. Purely command-line, no code written that you keep.

1. Manually export cookies from a browser after clicking through a real Blogger interstitial.
2. Run wget with `--load-cookies --keep-session-cookies --warc-file` against three or four post URLs.
3. Open the WARC with `warcio` and confirm the payload is real content, not the interstitial.
4. Index it with `cdxj-indexer`, serve with `pywb`, and browse it.
5. Confirm `wget --version` includes `+pcre` on your intended base image.

**Go/no-go:** if the cookie approach doesn't get past the interstitial, the answer is the browser-based path ([06 mode 3](06-access-profiles.md#mode-3--interactive-m5)) and M5 moves to the front. Better to learn that in an afternoon than in month three.

Also worth doing: run `--warc-dedup` against a second capture and verify you get revisit records. Incremental feed capture ([08](08-feeds-and-scheduling.md#incremental-captures)) depends on it.

---

## M0 — Foundation & auth ✅ **complete**

**Ships:** a container you can log into.

> **Status.** Built and verified: `docker run` → healthy container → create account → log in → dashboard, session surviving a restart. 66 tests, mypy strict and ruff clean. Four things the plan got wrong, corrected in place:
>
> - **wget PCRE detection.** `wget --version | grep +pcre` fails a *working* build — Debian's wget 1.25.0 links PCRE2 and honours lookahead but advertises neither `+pcre` nor `-pcre` (the flag only ever described PCRE1). The image now compiles a real lookahead pattern instead. See [04](04-discovery-and-scoping.md#translation-to-wget).
> - **Alembic's `fileConfig` hijacked logging**, so every app log after startup lost its JSON envelope and redaction. `env.py` now only configures logging when driven from the CLI.
> - **`env.py` overwrote the caller's database URL** with the process-wide cached settings, silently migrating the wrong database whenever settings were passed explicitly. Invisible in production, where the two coincide.
> - **`extra={"name": ...}` crashes stdlib logging** (reserved `LogRecord` attribute). A `SafeLogger` subclass now renames collisions rather than raising.

- Repo layout, Dockerfile, s6 services, CI (lint, type-check, test, image build)
- FastAPI skeleton, config loading, structured logging
- SQLAlchemy models + first Alembic migration for the full schema from [03](03-data-model-and-storage.md)
- Auth: Argon2id, sessions, first-run setup, login rate limiting, audit log
- Secret sealing (`CAIRN_SECRET_KEY`, AES-GCM) and the refuse-to-start check
- React + Vite shell, routing, generated API client, login page, empty dashboard
- Unraid template; `/api/health`

**Done when:** `docker run` on Unraid, create an account, log in, see an empty dashboard. Restart the container; the session persists and migrations are idempotent.

**Don't skip:** migrations and secret sealing. Both are miserable to retrofit and both are invisible until they hurt.

---

## M1 — Capture core ✅ **complete**

**Ships:** point it at a URL, get a WARC.

> **Status.** Built and verified end to end: add a site in the UI, press Capture, watch URLs stream past in the live log, get a WARC whose payload is real page text — read back with `warcio`, not merely checked for existence. 203 tests pass on Linux with real wget; mypy strict and ruff clean.
>
> **The flagged risk is closed.** The asset-host translation works: a reject regex that blocks pages on an assets-only host still lets `--page-requisites` pull that host's images. Settled by running wget 1.25.0 against a two-host fixture, not by reading the manual.
>
> Six things the plan got wrong, corrected in place:
>
> - **`--domains` is a hard gate that `--page-requisites` does not bypass.** Leaving an asset host out of `--domains` drops its images entirely, so asset hosts must be listed *and* fenced by the regex. There is no regex-free formulation ([04](04-discovery-and-scoping.md#translation-to-wget)).
> - **No regex over URLs can separate an extension-less image from an extension-less page.** The documented allowlist silently drops Blogger's proxied images. Hosts now carry an explicit `allow_extensionless` flag, defaulting off, with an `asset-audit` post-processor reporting referenced-but-missing assets so neither choice fails silently.
> - **wget does not de-duplicate a URL passed in both `--input-file` and on the command line.** [05](05-capture-engines.md#command-construction) showed both; the first real capture crawled the entire fixture site twice, at double the time and size, with no error.
> - **wget's `--warc-cdx` is written incrementally**, in lockstep with the log, so `url` events can stream with real status, MIME and digest rather than being reconciled after the crawl.
> - **A finished job's SSE stream never closed.** It replayed history then blocked forever, holding a connection and a task open for every completed job anyone opened.
> - **The app's own CSP blocked the app's own inline script** — the theme applied before first paint. A white flash for dark-mode users and a console violation, breaking nothing else, therefore invisible. The policy now carries a hash computed from the shipped file.
>
> **Confirmed against a live flagged blog.** A real Blogger site behind a content warning, a `cookies.txt` uploaded through the UI, the interstitial bypassed, and readable content in the resulting WARC. That is the milestone's exit criterion met in full, and it retires the M-1 spike's only remaining assumption: cookies alone are sufficient for this case, so the browser-based path stays in M5 rather than being pulled forward.

- Engine registry, manifest loading, JSON Schema validation
- Job supervisor: queue, spawn, NDJSON parsing, event fan-out, cancellation, crash recovery
- `wget-warc` engine, complete
- Access profiles, `cookies` mode only: upload, parse, validate, coverage report, encrypted storage
- Storage layer: site directories, capture directories, atomic writes, `site.yaml`, `manifest.json`
- Post-processors: `checksum`, `stats` (plus `manifest` and an advisory `asset-audit`)
- UI: add site, capture button, live log via SSE, capture list, URL list with errors

**Done when:** you archive a real Blogger site behind an interstitial, end to end, from the UI, and the WARC contains real content.

---

## M2 — Discovery & scoping ✅ **complete**

**Ships:** the domain picker — the feature that most distinguishes this from what exists.

> **Status.** Add a blog and press Index: it reads robots.txt, sitemaps and feeds, samples pages to find asset hosts, fingerprints the platform, and presents the picker with the Blogger case already correct. 272 tests pass on Linux with real wget.
>
> Five corrections, all from running it rather than reading about it:
>
> - **The PSL's private section solves multi-tenant grouping outright.** This document planned to flag `blogspot.com`-style suffixes and fall back to full-host grouping; `include_psl_private_domains=True` already returns `foo.blogspot.com` and `bar.blogspot.com` as distinct, and groups all four `N.bp.blogspot.com` as one CDN ([04](04-discovery-and-scoping.md#the-domain-picker)).
> - **`--delete-after` is incompatible with a seed list.** The on-disk mirror is how wget remembers what it has fetched, so discarding it makes every extra seed re-crawl the whole site — 4.8× the records for six seeds, and quadratic in the size of a real blog. Invisible with one seed, which is why M1 never saw it ([05](05-capture-engines.md#--delete-after-is-incompatible-with-a-seed-list)).
> - **A link to a file is not a link to a page.** Blogger's lightbox wraps every image in an anchor to the full-size file, which made the image CDN look like a site with thousands of inbound links.
> - **`?page=N` sitemap pagination needs a no-new-URLs guard.** It is a Blogger convention, not a spec; a server that ignores the parameter serves page 1 sixty times over.
> - **An HTML error page is not an empty sitemap.** It parses as well-formed XML and yields nothing, which is indistinguishable from a site that has none unless the root element is checked.
>
> And one bug: stripping `<script>` elements before reading attributes took their own `src` with them, so every external script was invisible to both discovery and the post-capture gap report.

- Discovery engine: robots, sitemaps (paginated + index), feeds, bounded sampling crawl
- Platform fingerprinting + Blogger preset
- Host classification: PSL grouping, link/asset ref counting, role guessing
- Scope resolution and persistence; scope preview with estimates
- UI: domain picker table with dual checkboxes, bulk actions, sample URL expansion, preset application
- Seed injection from sitemaps and feeds into captures

**Done when:** adding a Blogger blog auto-discovers `*.bp.blogspot.com`, preselects it as assets-only, excludes analytics, applies the `?m=1` reject, and the resulting capture stays inside those bounds.

---

## M3 — Replay

**Ships:** browsing the archive in the UI. The payoff milestone.

- `cdxj-index` post-processor; site-relative paths; atomic index swap
- pywb sidecar, generated config, collection-per-site keyed by ID, reload on change
- Iframe embed with app-supplied chrome: URL bar, capture selector, version count via the CDX API
- Origin separation, CSP, sandbox attributes, same-host startup warning
- Raw record inspector
- Rebuild-index action

**Done when:** you click through a fully archived blog inside the UI — pagination, images, CSS — and can switch between captures of the same page.

---

## M4 — Organization

**Ships:** folders, tags, filtering. The thing ArchiveBox couldn't do.

- Folder tree: create, rename, reparent, drag-and-drop; directory moves as jobs
- Tags with colors and autocomplete; bulk tagging
- Filter bar with compound queries; saved smart views
- `symlink-tree` post-processor for `/data/by-tag`
- Site cards with thumbnails, sizes, last-capture, status
- Trash with restore and scheduled purge
- Storage overview per folder and per site

**Done when:** twenty sites organized into a nested tree with tags, filterable in the UI, and the same structure is navigable on disk over SMB.

---

## M5 — Access profiles, complete

**Ships:** userscripts and interactive login.

- Chromium (Playwright) added to the image
- `mint` engine: userscript injection, `GM_*` shim, success detection, screenshot artifact
- Userscript metadata parsing with warnings for unsupported `@grant`/`@require`
- Auto re-mint on expiry and on `interstitial_detected`
- Interactive profile mode: browser session, noVNC/CDP embed, save cookies + storage
- Profile test/verify with per-site coverage checking

**Done when:** you upload a Tampermonkey script, the tool mints a working cookie jar from it, and a capture using that profile returns real content. And separately: you click through an interstitial in the embedded browser, save the profile, and it works the same way.

**Move earlier if** the M-1 spike showed cookies alone don't work.

---

## M6 — Feeds & scheduling

**Ships:** archives that stay current unattended.

- `feedparser` integration; auto-discovery; feed test endpoint with scope checking
- Conditional GET, GUID dedup, URL canonicalization, backoff
- APScheduler; per-feed intervals; jitter; quiet hours; per-host serialization
- Incremental captures with `--warc-dedup`; batching
- Sitemap-diff watcher
- Feeds UI with poll history
- Notifications (ntfy / Apprise / webhook)

**Done when:** a new post appears on a watched blog and is archived into that site's folder within the poll interval, at a fraction of a full capture's cost, with a notification.

---

## M7 — Engine SDK & second engine

**Ships:** the extension system, proven by using it.

- `cairn-engine-template` repo with a working example
- `cairn engines validate` and a protocol conformance test harness
- Docker runtime type (opt-in, socket access clearly flagged)
- `browsertrix-crawler` engine: profiles, behaviors, WACZ ingest
- Per-site engine selection with capability-aware UI (hide JS-dependent options on non-JS engines)
- Engine documentation

**Done when:** a JS-heavy site with lazy-loaded images that wget captures badly is captured correctly by browsertrix, selected per-site in the UI, with both engines' captures replaying from the same collection.

---

## M8 — Depth & polish

**Ships:** the things that make it good rather than sufficient. Pick by what you actually want.

| Feature | Value | Effort |
|---|---|---|
| Full-text search over extracted text (SQLite FTS5) | High | Medium |
| WACZ export + ReplayWeb.page sharing | High | Low |
| Integrity verification job + repair reporting | High | Low |
| `yt-dlp` media post-processor | Medium | Low |
| Retention policies (prune superseded captures) | Medium | Medium |
| Diff view between captures of a page | Medium | Medium |
| Page-change watcher for feedless sites | Medium | Medium |
| `single-file-cli` supplementary engine | Medium | Low |
| Import from ArchiveBox | Medium | Medium |
| Prometheus metrics | Low | Low |
| Public share links | Low | High |

Full-text search is the highest-value item on the list. Once you have hundreds of archived sites, "which of my archives mentioned this?" becomes the primary way you actually use the tool, and nothing else on the list changes day-to-day usage as much.

---

## Sequencing notes

**Why discovery before replay.** Discovery determines *what gets captured*; getting it wrong means recapturing everything later. Replay is read-only over whatever exists and can be built against any archive.

**Why organization after replay.** Folders and tags are only interesting once there's a body of archives worth organizing. Building them against three test sites means designing for the wrong scale.

**Why the engine SDK is M7, not M1.** The *interface* ships in M1 ([D3](00-decisions.md#d3--wget-for-v1-behind-an-engine-interface-from-day-one)); the SDK, docs, template repo, and conformance harness are only worth writing once a second real engine has proven the interface is right. Publishing an addon API before you've written a second implementation of it is how you end up supporting a bad API forever.

**Realistic scale.** For one person working evenings and weekends: M0–M1 is the bulk of the initial effort, M2–M4 each land in a few weekends, M5–M7 depend heavily on how cooperative Chromium and browsertrix turn out to be. M0–M4 is a genuinely useful tool; everything after that is improvement rather than viability.

---

## Definition of done, applied throughout

A milestone isn't done until:

- The feature works from the **UI**, not just the API (R1 is a requirement, not a preference)
- Errors surface in the UI with something actionable, not a spinner that stops
- It survives a container restart mid-operation
- It's covered by a test that would catch a regression
- The docs in this folder reflect what was actually built, including where it diverged from the plan

That last point is the one that decays first and matters most. These documents are the design; when the implementation teaches you something they got wrong, fix the document in the same commit.
