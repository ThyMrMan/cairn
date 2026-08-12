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

## M3 — Replay ✅

**Ships:** browsing the archive in the UI. The payoff milestone.

- [x] `cdxj-index` post-processor; site-relative paths; atomic index swap
- [x] pywb sidecar, generated config, collection-per-site keyed by ID, discovered without a restart
- [x] Iframe embed with app-supplied chrome: URL bar, capture selector, version count
- [x] Origin separation, CSP, sandbox attributes, same-host startup warning
- [x] Raw record inspector
- [x] Rebuild-index action

**Done when:** you click through a fully archived blog inside the UI — pagination, images, CSS — and can switch between captures of the same page. *Asserted end to end in `test_replay_e2e.py`: a real capture, a real pywb, the page read back out of the iframe's own URL, both captures of one page reachable, and subresources rewritten into the archive rather than fetched from the live site.*

**Five corrections from building it, all found by running the thing:**

1. **`collections_root` beats an explicit collection list.** pywb picks up a collection created while it is running, so adding a site needs no restart and the app never has to reach into the service supervisor. docs/07 had specified the explicit form.
2. **`cdxj-indexer` records basenames unless given `dir_root`,** and every capture writes `part-00000.warc.gz`. Without it, switching captures — this milestone's exit criterion — returns 503, and only from the second capture onwards. The builder now refuses an index whose filenames are not the paths it passed in.
3. **`frame-src` fell back to `'none'`** on every install without `CAIRN_REPLAY_PUBLIC_URL`, which is every default LAN install: our own CSP blanked the replay tab. The iframe URL and the policy now come from one function.
4. **pywb 2.9.1 imports `pkg_resources`,** removed in setuptools 81. Unpinned, replay is simply absent while everything else works.
5. **pywb ships a top-level `tests` package,** and a regular package beats a namespace directory wherever it sits on the path. Every `from tests.conftest import …` resolved to pywb's tests inside the container while passing on a development machine. `tests/__init__.py` settles it.

---

## M4 — Organization ✅

**Ships:** folders, tags, filtering. The thing ArchiveBox couldn't do.

- [x] Folder tree: create, rename, reparent, drag-and-drop; directory moves
- [x] Tags with colours; bulk tagging
- [x] Filter bar with compound queries; saved smart views
- [x] `/data/by-tag` symlink tree
- [x] Site cards with tags, sizes, last-capture, status
- [x] Trash with restore and a retention sweep
- [x] Storage overview per folder and per site

**Done when:** twenty sites organized into a nested tree with tags, filterable in the UI, and the same structure is navigable on disk over SMB. *Asserted in `test_organization_e2e.py`: twenty sites across a nested tree, compound folder-and-tag queries returning strictly fewer than either side alone, every folder a real directory at the path the API reports, and every `by-tag` entry a **relative** symlink that resolves to a real site.*

**Five corrections from building it:**

1. **A move is not slow enough to be a job — except when it is.** Inside one filesystem a directory move is one `rename(2)` and finishes before the response is written, whatever it holds; returning `202` for that means a progress bar for work already done. The slow case is real but narrow — different filesystems, which on Unraid means `/data` spanning array disks — so `rename_directory` raises `CrossDeviceMoveError` rather than falling back silently, and only that case becomes a job. docs/09 had specified `202` unconditionally.
2. **The copy fallback has to be staged.** `shutil.move` does copytree-then-rmtree onto the target, so a container stopped mid-copy leaves a half-written directory that looks like a real archive beside a partly-deleted source. Writing to `.moving-<name>` and renaming into place makes the interrupted state recoverable and sweepable.
3. **`by-tag` links must be relative.** An absolute `/data/…` link works perfectly in the container and resolves to nothing on a client that mounted the share, and Samba refuses it outright as a wide link — so the one place this feature is *for* is the one place it would fail. The exit criterion asserts non-absoluteness directly.
4. **The tag tree is rebuilt whole, not incrementally.** docs/03 called for a debounced refresh; a full rebuild is a few hundred `symlink(2)` calls, so incremental cost the same and added the only failure mode that matters — a tree that silently stops matching the database.
5. **There is no `symlink-tree` post-processor, and there should not be.** This list called for one, but a capture cannot change a site's tags, so it would have been a no-op on every capture and absent from every operation that does change them. The rebuild hangs off tag, folder and site changes instead, plus boot.

**A sixth, found after shipping, from a field report.** A tagged site appeared under `by-tag` as a 0 KB *file* rather than a folder. Symlinks carry a type — file or directory — decided when they are created and inferred from the target, and `create_site` rebuilt the tag tree *before* it created the site directory. Linux resolves by path at every access, so nothing in 366 tests or on any Linux box could see it; a Windows-backed filesystem bakes the type in and shows a file forever. Worse, the rebuild could not repair it: `_link` left alone any link whose text already matched, and the text was never the part that was wrong. Both halves are fixed and both are now pinned by tests, one of which fails on Linux if the ordering is reintroduced.

**Left out on purpose:** thumbnails, which this list also asked for. They need a screenshot, which needs a browser, which arrives in M5. Site cards carry tags, size, URL count, last capture and status instead of a picture that would have to be a placeholder.

**One thing to know:** the retention sweep runs at boot and on demand, not on a timer — there is no scheduler until M6. The window is a floor on how long a deleted archive is kept, never a promise about when it goes.

---

## M5 — Access profiles, complete ✅

**Ships:** userscripts and interactive login.

- [x] Chromium (Playwright) added to the image
- [x] The mint: userscript injection, `GM_*` shim, success detection, screenshot artifact
- [x] Userscript metadata parsing with warnings for unsupported `@grant`/`@require`
- [x] Auto re-mint on expiry, and interstitial detection after a capture
- [x] Interactive profile mode: browser session, CDP screencast embed, save cookies + storage
- [x] Profile test/verify with per-site coverage checking

**Done when:** you upload a Tampermonkey script, the tool mints a working cookie jar from it, and a capture using that profile returns real content. *Asserted in `test_access_e2e.py`: a real Chromium runs the script, a real wget runs the crawl with the jar it produced, and the page text is read back out of the WARC — with the control case asserted too, that the same capture without the profile archives the interstitial and is marked `partial`.*

**Six corrections from building it:**

1. **The interactive browser needs no noVNC.** docs/06 planned Xvfb, a VNC server and websockify. A CDP screencast does the same job headless at ~8 KB a frame and ~75 KB/s, so none of that stack is in the image. Its trap: frames are emitted only on *visual change*, so a settled page streams nothing and looks exactly like a broken socket.
2. **`interstitial_detected` cannot pause and resume a capture.** wget reads `--load-cookies` once at startup; there is no way to hand a running crawl a new jar. Re-minting moved to before the capture, and detection to after it, where the honest response is to downgrade the capture to `partial` and say so.
3. **`--no-shell` and `channel="chromium"` go together.** Playwright's default headless mode runs a separate 262 MB headless shell; installing without it makes a plain `launch()` fail outright. The build-time probe caught this on its first run.
4. **`PLAYWRIGHT_BROWSERS_PATH` is mandatory.** Otherwise the browser installs under the *building* user's home while the container runs as `abc` with `HOME=/config` — a mounted volume — so it would not be missing at build time, it would appear to vanish on a fresh install.
5. **Match patterns carry ports in practice.** Chrome's do not, Tampermonkey's do, and reading `host:port` as a hostname makes every such pattern match nothing — surfacing as "the script never ran".
6. **A WARC contains the cookies that fetched it.** Request records carry the `Cookie:` header. Not fixable and not previously written down; now warned about before any capture whose profile holds account session cookies (docs/11).

**Image cost:** 446 MB → 1.7 GB, against this document's ~500 MB estimate. Kept as one image rather than splitting `latest` and `full`: a button that silently does nothing because you pulled the wrong tag is a worse failure than a larger pull.

---

## M6 — Feeds & scheduling ✅

**Ships:** archives that stay current unattended.

- [x] `feedparser` integration; auto-discovery; feed test endpoint with scope checking
- [x] Conditional GET, GUID dedup, URL canonicalization, backoff
- [x] Per-feed intervals; jitter; quiet hours; per-host serialization — **not APScheduler**, see below
- [x] Incremental captures with `--warc-dedup`; batching
- [x] Sitemap-diff watcher
- [x] Feeds UI with poll history
- [x] Notifications (ntfy / Apprise / webhook)

**Done when:** a new post appears on a watched blog and is archived into that site's folder within the poll interval, at a fraction of a full capture's cost, with a notification. *Asserted in `test_feeds_e2e.py`: a fixture blog gains a post while the test is running, a real poll finds it, a real wget captures it, the new post's text is read back out of the WARC, the capture directory sits beside the full one in the same site folder, wget's own log proves it started from the new post rather than the site, and a real socket receives the notification. Both regressions it guards were reintroduced and confirmed to fail it.*

**Six corrections from building it:**

1. **APScheduler is the wrong tool, and its defaults are the reason.** A persistent job store is a second copy of a schedule the `feeds` table already holds, so every interval change is two writes that can disagree. Measured against 3.11 on SQLite: restarting across a fire time **drops the run** at the default `misfire_grace_time`, and with grace disabled and `coalesce` off a 30-second outage of a 3-second job fired **12 times at once**. Both failures are silent, and a container on Unraid restarts routinely. A due-time query has neither problem and costs only the cron syntax nothing here needs.
2. **The first poll of a feed has to be a baseline, not a backlog.** Every entry is new the first time it is read, so capturing them meant adding a watch to a blog and immediately re-fetching its whole archive one post at a time — the most expensive possible way to get what one full capture already covers.
3. **wget writes no CDX line for a URL `--warc-dedup` deduplicated.** Measured: a second crawl of a four-page site wrote four revisit records and a CDX containing only its header. So handing the *previous* capture's CDX to the next run means it deduplicates against an empty file — the saving holds for exactly one capture, then silently alternates on and off. The dedup source is now the union of every prior capture's CDX. The same finding, inverted, made an incremental capture report **zero URLs** and an empty URL list while its WARC was full; the engine now reconciles the CDX against wget's crawl log and emits the difference as revisits.
4. **Absence means opposite things in a feed and a sitemap.** A feed carries the most recent N entries, so an entry leaving one is the feed working correctly; a sitemap is meant to be complete. Disappearance — the "a post you archived no longer exists upstream" notification, the one worth having — is therefore only ever inferred from a *complete* sitemap read. Inferring it from feeds would fire on every poll of every healthy blog.
5. **Per-host serialization belongs in the supervisor, not the scheduler.** It is politeness rather than scheduling, so a capture somebody started by hand owes it too; two simultaneous crawls of one blog is what gets an archiver blocked, whoever started them.
6. **Quiet hours default to off.** docs/08 asked for 01:00–07:00 on by default, which would mean adding a feed, watching a post appear, and seeing nothing for eighteen hours — while throttling only an incremental capture of a few hundred kilobytes, since full recapture is off by default anyway.

**Left out on purpose:** the monthly discovery refresh from docs/08's schedule table. Re-running discovery can change a site's scope, and doing that unattended is a decision rather than maintenance. Log rotation is also absent and should be: logs go to stdout for s6 and Docker to handle.

**One thing to know:** the M4 note that the retention sweep runs only at boot is now retired. Trash purge and the size rollup are on the ticker, daily and hourly.

---

## M7 — Engine SDK & second engine ✅

**Ships:** the extension system, proven by using it.

- [x] Engine template with a working example — `examples/engine-template/`, in-repo rather than a separate repo
- [x] `cairn engines validate` and a protocol conformance test harness (`cairn engines test`)
- [x] Docker runtime type (opt-in, socket access clearly flagged)
- [x] `browsertrix-crawler` engine: behaviors, WARC ingest — **not profiles**, see below
- [x] Per-site engine selection with capability-aware UI
- [x] Engine documentation

**Done when:** a JS-heavy site with lazy-loaded images that wget captures badly is captured correctly by browsertrix, selected per-site in the UI, with both engines' captures replaying from the same collection. *Asserted in `test_browsertrix_e2e.py`: a real wget and a real browsertrix container crawl the same fixture, the WARCs are read back with warcio — wget gets **none** of the three lazy images and never sees the script-generated link, browsertrix gets all four — and the site's single CDXJ index is checked for WARCs from both captures.*

**Six corrections from building it:**

1. **The daemon resolves mounts on the *host*, so an engine's paths cannot be written by its author.** Cairn's `/data` is not the daemon's `/data`. Probed on a real daemon: our `/data` came from a named volume at `/var/lib/docker/volumes/…/_data` and our `/config` from `/run/desktop/mnt/host/c/Coding/Website Backup`, space and all. Cairn works the mounts out itself from its own mount table, and the container always sees `/cairn/job` and `/cairn/out`.
2. **`--volumes-from` is the obvious fix and gives away too much.** It reproduces every one of our mounts at our own paths — tested, including a Windows host bind — but "every one" includes `/config`, the database and the master key. Precise subpath mounts leave `/config` invisible, which was verified rather than assumed.
3. **`runtime: docker` cannot run a stock third-party image.** docs/05 sketched exactly that, with templated arguments. A stock image emits its own log format, and the protocol says stdout is cairn NDJSON. Tools that do not speak it get an *adapter engine*; letting the runtime translate would give it an "and which log format?" field.
4. **browsertrix cannot take a cookie jar, and no bridge exists.** It has no cookie option; `--profile` wants a browser profile tarball, and one built with our Chromium is accepted and ignored because it runs **Brave** while we ship **Chrome for Testing**. Verified against a gated fixture: it archived the interstitial. So "profiles" is struck from the bullet list above, the engine declares `auth: [user_agent]`, and the picker warns before the capture.
5. **A relative `command` could never work.** An engine runs with the *job's* temp directory as its working directory, so `command: ["python3", "engine.py"]` — the obvious thing to write — resolved to nothing. Arguments naming files in the engine's own directory are now made absolute. Found by the conformance harness on its first run against the template.
6. **Do not override a container's working directory.** browsertrix sets `WORKDIR /crawls` and resolves its output tree from there; pointing it elsewhere wrote the crawl where nobody was looking and still exited 0, reporting two pages crawled and no archive.

**Also measured:** `--behaviors` defaults to `autoplay,autofetch,autoscroll,siteSpecific`, and passing a shorter list drops `autofetch` — three lazy images became one. And the fixture itself needed correcting: wget scans script *text* for anything shaped like a URL attribute, so a literal `href="/post.html"` inside a script was found by it, and a test meant to prove "only a browser sees this" was quietly proving nothing.

**Left out on purpose:** the `single-file-cli`, `yt-dlp`, `wget2` and `warcprox` engines from docs/05's candidate list. The interface is proven by a second engine that exercises every part of it; a third that exercises the same parts again proves nothing further, and each is a real maintenance cost.

**One thing to know:** the container-engine tests need the Docker socket *and* `CAIRN_TEST_CONTAINERS=1`. They pull most of a gigabyte and take minutes, and a CI runner has a socket — so the second gate is a deliberate opt-in rather than something every push pays for.

---

## M8 — Depth & polish ✅ *(the three high-value items)*

**Ships:** the archive becomes searchable, portable and checkable.

- [x] Full-text search over extracted text (SQLite FTS5)
- [x] WACZ export + ReplayWeb.page sharing
- [x] Integrity verification job + repair reporting

**Done when:** you can ask "which of my archives mentioned this?" and get the post rather than the blog; hand somebody one file that replays without a server; and find out that a WARC changed on disk before you need it. *Asserted in `test_search_e2e.py` against a real wget crawl of a blog whose sidebar lists every post title on every page: searching one of those titles returns the post and the index that genuinely lists it, never the four unrelated posts. The export is handed to the pywb already in the image, which unpacks it, reads our index and serves a page back out of our archive member. And one byte is flipped in an archived WARC, after which the verify job names the file, the capture and the site.*

**Five corrections from building it:**

1. **A contentless FTS5 index costs a fifth of an ordinary one, and the difference is paid eleven times.** Measured over the same corpus: `content=''` is 0.21x the raw text, a plain FTS5 table 1.29x. The database is copied whole before every migration and ten backups are kept, so the ordinary form turns a gigabyte of extracted text into nearly thirteen on the cache pool — for a second copy of data that is already on the array and regenerable from the WARCs. Contentless costs `snippet()`, which returns NULL, so snippets are built by seeking into `derived/text/`. Two more measured constraints came with it: `contentless_delete=1` is what makes DELETE possible at all, and even with it SQLite rejects an UPDATE of a subset of columns, so a re-captured page is deleted and re-inserted.
2. **A virtual table is five tables, and Alembic wants to drop all five.** `page_text_fts` brings `_data`, `_idx`, `_docsize` and `_config`, none of which can exist in `Base.metadata` — so `compare_metadata` reports five `remove_table`s and an autogenerated revision would open by deleting the search index. `include_object` in `env.py` filters them, and the M6 schema-drift test uses the same filter, which is why it lives in `db/base.py` rather than in the migration environment: that module runs migrations on import.
3. **Boilerplate is the whole of search quality, and one filter is not enough.** A blog's sidebar lists every post title on every page, so indexing what was served makes one post title match the entire blog. Class and id names catch Blogger and WordPress — measured against `trafilatura`, the same article, no sidebar, no nav, and the `<title>` kept, which trafilatura's extractor drops. A template that names its columns `left` and `right` defeats them entirely, and there trafilatura wins. The second filter is the one neither has: we index a whole capture at once, so boilerplate is *the blocks that appear on most of its pages*. With both, no HTML parsing dependency is needed at all — `trafilatura` brings lxml, justext, courlan, htmldate and dateparser to do a job the stdlib parser plus knowledge of the corpus does at least as well on the pages this tool archives.
4. **py-wacz is not a dependency worth having.** `wacz` 0.5.0 requires `black`, `pytest-cov` and `frictionless`, and frictionless pins `jsonschema==4.17.3` while the engine registry needs `>=4.23`: installing it trades a working engine validator for a zip writer. The format is six entries and is written here instead. Its own validator, incidentally, reports failure on stdout and exits **0** regardless — usable as a cross-check only if you read the text.
5. **Reading a WARC end to end does not detect truncation.** The deep verification pass exists for the one thing a checksum cannot cover: a WARC that was already broken when it was checksummed, because the container stopped mid-write. Measured: a four-record file missing its last forty bytes parsed all four records and reported nothing, and one cut in half parsed two and reported nothing. So the pass also insists the compressed stream reaches its end-of-stream marker, which is what actually catches it.

**Also corrected, in docs/07:** **pywb does not serve a `.wacz` in place.** Three configurations against the pinned 2.9.1 all 404, and `wb-manager` says so outright — *"Adding waczs without unpacking is not yet implemented."* It imports one with `--unpack-wacz`, which is how the export is tested and is a better proof anyway: an independent reader resolving our offsets. And **every WARC in a WACZ needs a unique basename**, because the index names files by basename alone while every capture writes `part-00000.warc.gz`. Reintroducing the collision produces an export whose checksums are fine and whose index resolves to the wrong records — *"that offset holds … instead"* — which is exactly what `/exports/{name}/verify` reports.

**Left out on purpose**, from this section's own menu: `yt-dlp`, retention policies, capture diffing, the page-change watcher, `single-file-cli`, ArchiveBox import, Prometheus metrics and public share links. The three built are the three the table calls high-value, and the last of those — share links — is the one most likely to introduce a security hole, on the origin that replays untrusted JavaScript.

**One thing to know:** search covers what has been extracted, and extraction runs after a capture. Captures made before M8 are absent from the index until **Rebuild search index** reads their WARCs again — which the search page says, with a button, rather than returning nothing and leaving you to guess.

---

## M8 continued — change over time ✅

**Ships:** knowing what changed, watching for change, and deciding what to keep.

- [x] Diff view between captures of a page, and a site-level summary of which pages changed
- [x] Page-change watcher for feedless sites
- [x] Retention policies (prune superseded captures)

**Done when:** a post is edited between two captures and the diff names the post and the sentence; a watched page with no feed is captured when its text changes and not when its visit counter does; and retention refuses to delete the capture holding the only copy of a post the author removed. *Asserted in `test_changes_e2e.py` against a real wget crawl of a fixture blog that is edited, watched and pruned while the test runs — including the negative case, that two captures of an unchanged site diff to zero changed pages while every response carried a different visit count.*

**Three corrections from building it:**

1. **Pruning a capture can destroy a capture it does not touch.** An incremental capture deduplicated with `--warc-dedup` writes a revisit record: a pointer with no payload. Measured against the pinned pywb — build two captures, delete the older, rebuild the index, and replay answers **503** for a page whose own capture directory is entirely present, whose WARC still passes its checksum, and whose index entry is still there. So "keep the last three captures" can destroy the three it keeps, and retention grew a fourth protection: a capture that later revisit records resolve into is never pruned. It is computed from the CDXJ, which is what replay itself resolves against.
2. **Diffing markup reports every page as changed, forever.** A visit counter, a rotating ad slot or a "generated at" stamp changes the response on every fetch: three consecutive fetches of one unchanged post produced three different body hashes and one identical extracted-text hash. Both the diff and the page watcher therefore work from the extracted text, which is already there for search. The same measurement is what decides the watcher's change signal.
3. **`last-copy` has to protect the *newest* capture holding a vanished URL, not every capture holding it.** Protecting all of them means one deleted post pins a site's entire history forever, and retention silently becomes a feature that never prunes anything. Walked newest-first with a running set of URLs seen so far, so exactly one capture is pinned per vanished page.

**Also worth knowing:** `difflib` is enough. Measured at 9 ms for a 60,000-word page at block level plus 8 ms of word-level work inside the blocks that changed, and 0.1 ms for two pages with nothing in common — so there is no diffing dependency either.

**Retention is off by default and its dry run works before it is switched on**, because the dry run is how anybody decides to switch it on. `apply` recomputes the plan inside the job rather than trusting the one a browser tab is showing, and reports every capture it refused to delete on that basis.

**Left out of this pass:** `yt-dlp`, ArchiveBox import and Prometheus metrics, all three built in the pass below.

---

## M8 continued — media, import, metrics ✅

**Ships:** the rest of the list, bar two.

- [x] `yt-dlp` media post-processor
- [x] Import from ArchiveBox
- [x] Prometheus metrics

**Done when:** a capture of a page with an embedded video has the video; a real ArchiveBox archive imports and replays; and `/api/metrics` parses as Prometheus expects. *Asserted in `test_media.py` and `test_import.py` — and the importer was additionally run against the output of a **real ArchiveBox 0.7.4**, which is where its schema came from in the first place.*

**Four corrections from building them:**

1. **ffmpeg costs 481 MB and buys very little here.** yt-dlp needs it only to *merge* separate video and audio streams; Debian's package is 481 MB across 200 packages, measured, against yt-dlp's own 25 MB. That is a 28% larger image to raise an archived clip from a muxed 720p to a merged 1080p, and the archival value is overwhelmingly in the clip existing at all. The default format asks for a single file that needs no merging, and a format string that does require merging fails with yt-dlp saying exactly that.
2. **Media URLs are the one attacker-controlled fetch target in the system.** Every other URL — a seed, a feed, a `verify_url` — is one the single user typed. These come out of archived HTML somebody else wrote, so docs/11's private-range block is enforced here and only here, checking *every* address a host resolves to. The same reasoning that exempts notification webhooks applies in reverse: a seed pointed at a LAN wiki is a choice, an embed pointed at a router is not.
3. **`ArchiveBox.conf` holds a `SECRET_KEY` and no version.** The first version of the survey read that file looking for a version string — found none, and would have been handling somebody's Django secret to learn nothing. The layout is detected from the tables instead.
4. **The manifest was recording less about a capture that went well than one that did not.** It is written at order 35 so that a chain dying partway still leaves one, and it used to be rewritten at the end *only when there were warnings* — so the index record count, the extracted text and the media results reached disk only on captures that had something wrong with them. Found by a media test asserting the manifest, not by the media feature itself. It is now always rewritten.

**Also worth knowing:** one bad entry must not abandon an import. A real archive accumulated over years has snapshots nobody remembers adding, and the first run against a real ArchiveBox died on one whose host was a Docker network alias rather than a hostname. Each domain is now imported independently, and the ones that cannot be are reported and skipped.

**A domain becomes a site and the whole import becomes one capture.** One capture per snapshot would give a domain with five hundred archived pages five hundred captures of one page each. Nothing is lost by grouping: the CDXJ records each response's own date, and replay's time dimension comes from the index rather than from the directory a WARC sits in.

**Left out of M8, deliberately and finally:** the `single-file-cli` engine — M7 already recorded why a third engine exercising the same interface proves nothing further — and **public share links**. That one is `Low` value and `High` effort by this document's own table, and it is the feature most likely to introduce a security hole: it deliberately punches a hole in the auth boundary, on the origin that replays untrusted JavaScript. It should be built, if at all, as its own piece of work with the constraints in [13](13-feature-backlog.md#public-share-links) in front of you, not as the tail end of a milestone.

---


## After M8 — the backlog's Tier 2 ✅

**Ships:** discovery that can see JavaScript, sites that span domains, and a report about what has *not* happened.

- [x] Browser-based discovery ([13](13-feature-backlog.md#browser-based-discovery))
- [x] Multi-seed sites ([13](13-feature-backlog.md#multi-seed-sites))
- [x] Scheduled report digest ([13](13-feature-backlog.md#scheduled-report-digest))

**Done when:** a host that only JavaScript names appears in the domain picker; a blog that moved to a new domain is one site with one index; and a site nothing has captured in three months says so without anybody going looking. *Asserted in `test_discovery_browser.py` against four loopback addresses each reachable by a different route — the network log, a script-injected link, and a scroll — with the fetch-only run as the control; in `test_multiseed.py`, where two domains are enumerated separately and the picker is saved without losing the second; and in `test_digest.py`, where a tick sends a real notification to a real socket naming a site that has been silent for 200 days.*

**Three corrections from building them:**

1. **Rendering a page and re-parsing the DOM does not find a JavaScript-only host.** It is the obvious implementation of "discovery through a browser" and it misses the case the feature exists for: `new Image()` fetches without ever entering the document. Measured on a fixture with one host per route — the rendered DOM found two asset hosts, the browser's own **network log** found three, and the missing one was the pixel. The log is the evidence; the DOM supplies links and nothing else. The log also carries each response's real content type, which the fetch path never learns for anything it did not fetch itself.
2. **The domain picker deletes what it does not know about.** It submits hosts and patterns and no seeds, so `save_scope` replacing `scope_settings` wholesale dropped every seed after the first — and `user_edited` with it, which is what stops the next re-index overwriting a hand-picked scope. Both were live bugs the moment multi-seed existed, and the second was a latent one before that. `save_scope` now merges.
3. **A digest of what happened is a digest nobody needs.** docs/13 asked for captures, new posts, failures and growth — all of which the app already shows on its own pages. What nothing shows is *absence*: the feed that polls successfully and returns nothing because the URL now serves a login page, the site whose captures quietly stopped. Those are what the report leads with, and the failing jobs are named rather than counted, because a count only sends the reader to the job list to find out what it counted.

**Also worth knowing:** rendering is capped at 40 pages however many were asked for, and each run reports whether the browser found anything the HTML did not already name — "this site does not need the browser for discovery" is the sentence that saves the next hour. The digest waits a full period before its first send, because a report an hour after installation says nothing and teaches the reader to ignore the next one.

---

## After M8 — reading, liveness, and getting things in ✅

**Ships:** an archive you can read, that tells you when the original disappears, and that you can fill from a bookmark export.

- [x] Read-only reader view ([13](13-feature-backlog.md#read-only-reader-view))
- [x] Site health monitoring ([13](13-feature-backlog.md#site-health-monitoring))
- [x] Bulk URL import ([13](13-feature-backlog.md#bulk-url-import))

**Done when:** an archived post reads as an article with no pywb involved; a site that starts returning 404 says so without anybody going to look; and pasting a bookmark export produces one site per domain and archives exactly the pages listed. *Asserted in `test_reader_health_import.py`, including the three negatives that matter: a reader page written before block kinds existed still reads, one 404 does not mark a site as gone, and importing three URLs across two domains queues two captures that crawl nothing.*

**Three corrections from building them:**

1. **Two positional lists, filtered separately, is a silent corruption.** The reader needed to know what each block *was*, which meant a `kinds` array beside `blocks` in the extracted text — and the boilerplate filter drops blocks. Dropping one without its kind gives every heading after it the kind of the one before: a page whose text is entirely right and whose structure is quietly wrong, which no test of the text would catch.
2. **A 500 is a site failing, not a site ending — and one 404 is neither.** The naive check reports whatever the last request said, which over a month of ordinary internet turns "this blog is gone" into a notification people mute. A state changes only after two checks agree, `unreachable` is kept apart from `gone` because the action is "check your network", and `blocked` is kept apart from both because a 403 is about our user agent.
3. **A pasted URL is a page, not a site.** Seeding a site at `blog/2019/03/some-post.html` gives an archive whose identity is one post and whose scope is derived from it, so a group's site is seeded at the origin instead — and therefore the capture must not crawl, because fifty bookmarks across fifty domains each triggering a full crawl is a plausible way to get an IP address blocked. Grouping by registrable domain brought a third: a group can span hosts, and a scope built from the first URL's host silently drops everything on the other.

**Also worth knowing:** the reader is checked *before* the replay index is, because it reads extracted text rather than the CDXJ and is therefore exactly the view that still works when the collection will not load. And the URL parser takes every http(s) address out of whatever was pasted, so a Netscape bookmarks export, a markdown list and a spreadsheet column all work without a format selector — or a parser per format, each with its own way of being subtly wrong.

---

## After M8 — one click in, notes on the way out ✅

**Ships:** a bookmarklet, profiles that carry a whole browser session, and annotations.

- [x] Bookmarklet ([13](13-feature-backlog.md#bookmarklet--browser-extension))
- [x] Personas beyond cookies ([13](13-feature-backlog.md#personas-beyond-cookies))
- [x] Archive annotations ([13](13-feature-backlog.md#archive-annotations))

**Done when:** a bookmark on the bar archives the page you are reading and nothing else; a profile minted from a userscript keeps the localStorage half of a login and says which engines can use it; and a highlighted sentence is still highlighted in the next capture of that page. *Asserted in `test_annotations_personas.py`, including the two that matter most: a note whose sentence was deleted is reported rather than moved, and a note survives its block moving to a different index in a later capture.*

**Three corrections from building them:**

1. **The bookmarklet cannot carry a credential, and does not need one.** A `javascript:` bookmark runs on somebody else's origin, so an authenticated call to Cairn would need a token in the URL — in browser history, in the referrer, in every proxy log on the way. It opens a Cairn page instead and lets the session cookie already in that browser do the work. Server-side it is the URL importer with one URL, so it added no endpoint and inherited "this page only, do not crawl the site".
2. **Anchoring an annotation to replayed content is not hard; it is unavailable.** Replay is a separate origin precisely so archived JavaScript cannot reach the app — which means the app cannot read a selection out of the iframe either, and giving it one would undo the isolation docs/07 and docs/11 exist for. Annotations therefore live on the reader view and anchor to a *quotation*, which is the more durable anchor anyway: re-extraction rewrites every byte offset, and a later capture of the same page has different ones again.
3. **The context around a quote must be collapsed, not stripped.** The space between "mentions" and the quotation belongs to the context. Stripping it makes `before.endswith(prefix)` false for the very text the annotation was made in, so the disambiguation pass never matches and every ambiguous quote falls through to "the first occurrence" — silently moving notes to the wrong sentence, which is exactly what the context was added to prevent. Found by the test written for the feature, not by using it.

**Also corrected, in docs/13:** full browser state does **not** make a profile work with `browsertrix-crawler --profile`. M7 had already measured why — browsertrix runs Brave, this image ships Chrome for Testing, and a profile built with one is accepted and ignored by the other. What it is actually good for is the browser paths inside this application, plus one sentence the UI had no way to say before: wget takes `--load-cookies` and nothing else, so a login kept in localStorage passes the profile test and fails the capture.

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
