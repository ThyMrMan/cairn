# 01 — Requirements

Your stated requirements, each traced to a concrete design response and the doc that specifies it. Where a requirement has a hidden constraint or a decision embedded in it, that's called out.

---

## Functional requirements

### R1 — Web UI for all controls

**Response.** Every operation is available in the UI: adding sites, discovery, domain selection, capture, scheduling, engine config, access profiles, folders/tags, replay, settings. A CLI exists for scripting and debugging but is never *required* — no operation is UI-inaccessible.

**Implication.** Anything with a config file gets a UI editor with validation, not a "edit this YAML on disk and restart" instruction. Engine config forms are generated from each engine's JSON Schema so addons get UI for free ([05](05-capture-engines.md#config-schema--generated-ui)).

→ [02](02-architecture.md), [09](09-api.md)

---

### R2 — Single user, authenticated, safe to expose to the internet

**Response.** Local account with Argon2id password hashing, optional TOTP second factor, server-side sessions in `HttpOnly; Secure; SameSite=Lax` cookies with idle and absolute timeouts, login rate limiting with progressive lockout, and an audit log of auth events.

**Beyond login.** "Safe to expose" for *this* application means three additional things that a generic auth checklist misses:

1. **Replay executes untrusted JavaScript.** Archived pages run in your browser. Replay must be on a separate origin from the app so archived JS can't reach the session cookie or the API. This is the single most important security property of the system.
2. **The app fetches arbitrary URLs by design.** That's SSRF as a feature. Private address ranges are blocked by default with an explicit per-site override.
3. **It stores cookie jars**, which are credentials. Encrypted at rest, never returned by the API.

Deployment guidance is to front it with a reverse proxy + TLS, and preferably Tailscale, a Cloudflare Tunnel, or Authelia/Authentik rather than raw port-forwarding.

→ [11](11-security.md)

---

### R3 — Runs on Unraid via Docker

**Response.** One image, `linuxserver`-style `PUID`/`PGID`/`UMASK`, `/config` and `/data` volumes, healthcheck, and a Community Applications template XML. Two ports (UI + replay).

**Unraid-specific constraints that affect design.**
- SQLite on the FUSE `/mnt/user` layer with mover active is the classic corruption footgun — the DB and pywb indexes must be pinned to the cache pool; only the bulk WARC storage belongs on the array.
- Symlinks (used by the tag tree, D8) work fine on Unraid but must not point outside the container's bind mount, so the tag tree lives inside the same `/data` root as the archives.
- A future browser-based engine needs `--shm-size=2g`; the template should set it preemptively.

→ [10](10-deployment-unraid.md)

---

### R4 — Import cookies files **or** Tampermonkey userscripts, selectable per site

**Response.** An **Access Profile** is a first-class object with a mode:

| Mode | How it works | Available |
|---|---|---|
| `cookies` | Upload a Netscape `cookies.txt`; passed to the engine directly | M1 |
| `userscript` | Upload a `.user.js`; a headless-Chromium pre-flight runs it against the seed URL, then exports the resulting cookie jar | M5 |
| `interactive` | Log in / click through once in an embedded browser session; the resulting jar + storage is saved as a reusable profile | M5 |
| `none` | No auth material | M1 |

A site picks one profile. Profiles are reusable across sites (one Blogger profile can cover many blogspot sites, since the interstitial cookie is typically scoped to `.blogspot.com`).

**The constraint worth knowing up front.** wget does not execute JavaScript, so a userscript cannot run during a wget crawl. The `userscript` mode is therefore a *cookie-minting pre-flight*, not in-crawl injection. Every mode converges on "a cookie jar the engine loads," which is what makes the per-site selector meaningful rather than a mode that silently also changes your engine. This is [D4](00-decisions.md#d4--tampermonkey-userscripts-run-in-a-pre-flight-not-during-the-crawl).

**Practical notes for Blogger.** Interstitial cookies are frequently *session* cookies, which most browser exporters drop and wget won't persist without `--keep-session-cookies`. The user agent used to mint the cookie should match the one used to crawl. Both are handled by the profile, not left to the user.

→ [06](06-access-profiles.md)

---

### R5 — Initial index producing an organized list of associated domains

**Response.** A dedicated **Discovery** job that does not write WARCs:

1. Fetch `robots.txt` (for `Sitemap:` directives), `sitemap.xml` and its pagination/index children, and any `<link rel="alternate">` feeds.
2. Blogger-aware probes: `/sitemap.xml?page=N`, `/feeds/posts/default?alt=rss&start-index=N&max-results=500`.
3. Shallow bounded crawl of the seed host (configurable page cap and depth) to collect outbound hosts.
4. Group every discovered host by *registrable domain* using the Public Suffix List, not by naive string suffix.
5. Classify each host: does it serve page links, subresources, or both? How many references? What's the guessed role (CDN, images, fonts, analytics, social, comments)?

Results render as a sortable table with counts and sample URLs. See [04](04-discovery-and-scoping.md#the-domain-picker) for the exact classification rules and the Blogger preset.

→ [04](04-discovery-and-scoping.md)

---

### R6 — Select which discovered domains to back up, from the UI

**Response.** The discovery result is a checklist with per-host toggles at two levels: *crawl this host's pages* and *fetch this host's subresources*. These are different — you almost always want `1.bp.blogspot.com` images without crawling it as a site.

Sensible defaults are preselected (seed host: both; hosts that only ever appear as subresources of the seed: subresources only; known analytics/ad hosts: neither). Selections become a **scope rule set** attached to the site, translated per-engine (for wget: `--span-hosts` + `--domains` + `--exclude-domains` + `--accept-regex`/`--reject-regex`).

→ [04](04-discovery-and-scoping.md#from-selection-to-scope)

---

### R7 — Back up an entire domain, scoped to that domain, page by page

**Response.** A capture crawls unlimited depth within the scope rule set, unlike ArchiveBox's `--depth=0|1` ceiling (its issue 1). Scope boundaries are enforced by the engine, so a sidebar link to an unrelated domain is never followed (its issue 2). Discovered URLs from sitemaps and feeds are injected as additional seeds so sequential pagination (`page2` → `page3` → …) doesn't depend on hop-following at all.

Every fetched URL is recorded as a row with status, MIME type, size, and content digest, so the UI can show "1,847 pages, 12 errors" and let you inspect the failures.

→ [05](05-capture-engines.md#the-wget-warc-engine)

---

### R8 — wget + WARC first, with an extension system for more engines

**Response.** A documented engine contract: a manifest (`engine.yaml`) declaring capabilities and a JSON Schema for config, plus a job protocol (job spec JSON in, NDJSON events out, artifacts declared with checksums). Engines run as subprocesses or as separate Docker containers. Drop a directory into `/config/engines/` and it appears in the UI with a generated config form.

A second addon type, **post-processors**, hooks after capture: indexers, text extractors, screenshotters, WACZ packagers, notifiers.

Candidate second engines, in order of value: `browsertrix-crawler` (JS, lazy-load, profiles), `single-file-cli` (one-file HTML snapshots), `yt-dlp` (embedded media), `wget2` (faster).

→ [05](05-capture-engines.md)

---

### R9 — Folders, filtering, and tagging in the UI

**Response.** A nested folder tree (materialized path, drag-and-drop, unlimited depth), free-form tags with color and autocomplete, and a filter bar supporting compound queries (folder, tag, engine, status, host, last-captured range, size, has-errors) that can be saved as named smart views.

→ [03](03-data-model-and-storage.md#folders-and-tags), [09](09-api.md)

---

### R10 — Filesystem organized into folders

**Response.** The archive tree on disk mirrors the UI folder tree exactly: `/data/archives/<folder>/<subfolder>/<site-slug>/`. A site's captures, index, and metadata all live under its directory, so a site is one self-contained, movable, backup-able unit.

Because tags are many-to-one with files and can't be a primary layout, a generated `/data/by-tag/<tag>/<site-slug>` symlink tree provides tag-based filesystem navigation — the same workaround the original notes arrived at for ArchiveBox, but maintained automatically instead of by cron script.

→ [03](03-data-model-and-storage.md#on-disk-layout)

---

### R11 — View complete WARC archives in the UI

**Response.** pywb sidecar with one collection per site, embedded in the UI via iframe on an isolated origin. Full framed replay: navigate the archived site normally, with a timeline of available captures. A raw record inspector (headers, status, payload) sits alongside for debugging.

WACZ export makes an archive portable and viewable in `replayweb.page` with no server at all.

→ [07](07-replay.md)

---

### R12 — Associate RSS/Atom feeds so new posts land in the site's folder

**Response.** Sites carry zero or more feeds. A scheduler polls each on its own interval using conditional GET (ETag / `If-Modified-Since`). New entries are deduplicated by GUID with a URL fallback, then enqueued as an incremental capture into the same site directory, with `--warc-dedup` pointed at the previous capture's CDX so unchanged assets aren't re-stored.

**Learning from the notes.** ArchiveBox's feed parser is regex-based and chokes on Atom's `<link href>` form (its issue 3), which is Blogger's default. This uses `feedparser`, which handles RSS 0.9x/1.0/2.0, Atom 0.3/1.0, and malformed variants. The `&alt=rss` workaround is unnecessary but remains available as a per-feed override.

**Also offered, because feeds miss things.** Sitemap-diff watching (catches pages that never appear in a feed) and page-change watching for sites with neither.

→ [08](08-feeds-and-scheduling.md)

---

## Non-functional requirements

| | Target |
|---|---|
| **Deployment** | One container, one `docker run`, no external services |
| **Resource envelope** | Idle < 200 MB RAM; capture of a 2,000-page blog < 1 GB RAM, single core |
| **Crawl politeness** | Default 1 req/s with jitter, 1 connection per host, identifying UA, `robots.txt` respected |
| **Durability** | WARCs immutable once closed; checksummed; index regenerable from WARCs alone |
| **Recoverability** | Losing the DB loses metadata, not archives — a rebuild job can reconstruct sites and captures from disk |
| **Restart safety** | Interrupted jobs are detected on boot and resumable |
| **Portability** | Archives are standard WARC; nothing requires this tool to read them |

The durability/recoverability pair is worth designing for explicitly: **the database must never be the only copy of anything.** Each site directory carries a `site.yaml` and each capture a `manifest.json` with enough metadata to rebuild the DB row. This is what makes the archive outlive the tool.

---

## Explicitly out of scope for v1

Recording these so they don't creep in:

- Multi-user, roles, sharing, or public archive publishing
- Distributed or multi-node crawling
- Full-text search (M8 candidate — see [13](13-feature-backlog.md))
- Deduplication across *different* sites (within-site is in v1)
- Automatic paywall/CAPTCHA circumvention
- Mobile app (the UI should be responsive; that's it)
- Archiving anything that isn't HTTP(S)
