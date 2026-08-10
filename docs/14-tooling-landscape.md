# 14 — Tooling Landscape

Reference for every tool relevant to this project: what it does well, where it fails, and whether this design uses it. Extends the comparison table in the [original evaluation](../archivebox-notes-and-alternatives.md) with the tools that weren't covered.

---

## Complete archiving platforms

Tools that try to be the whole system. These are the alternatives to building anything.

### ArchiveBox

The original candidate. Django + a plugin-ish extractor system, official Docker images, community Unraid template.

**Good at:** breadth of output formats per URL (WARC, screenshot, PDF, singlefile, DOM, readability, media via yt-dlp), a large ecosystem, cookies via `COOKIES_FILE` and the newer personas system, native feed-URL input.

**Where it failed for this use case,** per the original notes: `--depth` maxes out at 1 so full-site crawling requires feeding a complete URL list; depth-following ignores domain boundaries without a regex allowlist; the feed parser can't read Atom's `<link href>` form (Blogger's default); sitemap `<loc>` extraction doesn't work as expected; one Snapshot per URL means hundreds of directories per blog; no way to browse a site as one archive; tags never touch the filesystem.

**Verdict:** not used, but studied closely — its data model is the specific thing this design inverts ([D1](00-decisions.md#d1--one-capture-job-per-site-not-per-url)). Worth an importer ([13](13-feature-backlog.md#import-from-archivebox)).

### Karakeep (formerly Hoarder)

Bookmark-manager-first, archiving-second. Docker install, no dedicated Unraid template.

**Good at:** the best organization and search UX in this category — full-text search, AI-assisted tagging, lists, a genuinely good browser extension, and RSS auto-hoarding as a first-class feature rather than a scheduler bolt-on.

**Gap:** no cookie import or custom-script support; open issue #414 on bypassing cookie/consent banners is unresolved. Can't get past the Blogger interstitial today. Archives are page snapshots, not crawls — no whole-domain capture.

**Verdict:** not used. Its search and tagging UX is the target to match ([13 tier 1](13-feature-backlog.md#full-text-search-across-all-archives)).

### Linkwarden

Bookmark manager with preservation. Docker, self-hosted, active development.

**Good at:** the closest thing to a real folder model — collections with sub-collections plus tags. Multiple preservation formats. Built-in RSS subscriptions. Sharing with expiry.

**Gap:** same as Karakeep — no cookie import, no custom scripts, no whole-site crawling.

**Verdict:** not used. Its collection model informed the folder design.

### Browsertrix (Webrecorder)

The professional-grade option. Full platform wants Kubernetes/microk8s; `browsertrix-crawler` alone is a friendly single Docker container.

**Good at:** the actual hard problems. Real Chromium, so JS, lazy-load, and infinite scroll all work. **Browser profiles**: log in once through a noVNC session, save the profile, every subsequent crawl starts authenticated — the strongest available answer to the Blogger interstitial. Behaviors for autoscroll and media autoplay. WACZ output. Genuine scope controls.

**Gap:** the full platform is far heavier than "one Docker container on Unraid." The bare crawler has no scheduling, no feed watching, no organization, no UI.

**Verdict:** **`browsertrix-crawler` is the planned second engine** ([M7](12-roadmap.md#m7--engine-sdk--second-engine)), and its profile concept is directly borrowed for the `interactive` access-profile mode ([06](06-access-profiles.md#mode-3--interactive-m5)). The full platform is not used.

### Conifer (formerly Webrecorder.io)

Hosted high-fidelity archiving with interactive capture sessions.

**Verdict:** not used — hosted, and interactive-capture-first rather than crawl-first. Its "record while you browse" model is a genuinely different and interesting approach worth remembering for pages nothing else can capture.

---

## Crawlers

### GNU wget

**Chosen for v1** ([D3](00-decisions.md#d3--wget-for-v1-behind-an-engine-interface-from-day-one)).

**Good at:** everywhere, stable, well-documented, first-class WARC support including `--warc-dedup` for cross-run deduplication, precise scope flags, trivially rate-limited, no runtime dependencies.

**Limits:** no JavaScript (lazy-loaded images are missed), single-threaded, memory grows with crawl size, `--reject-regex` needs a PCRE-enabled build for lookahead.

Full flag reference in [05](05-capture-engines.md#the-wget-warc-engine).

### wget2

wget's successor: multi-threaded, HTTP/2, HTTP compression, WARC support, actively developed.

**Verdict:** an easy speed win as a second engine. Flags differ enough that it's a separate engine, not a config toggle. Note that WARC support has historically been less battle-tested than wget 1.x's — verify dedup and segmentation behavior before trusting it with a large archive.

### browsertrix-crawler

See above. The JavaScript answer.

### grab-site

Archive Team's wpull-based crawler. WARC output, a live dashboard, dynamic ignore-pattern editing mid-crawl, and curated **ignore sets** — maintained pattern collections for the junk that appears on every site (session IDs, infinite calendars, sort permutations, printer-friendly duplicates).

**Verdict:** not used as an engine (wpull is less actively maintained), but **the ignore sets are worth mining directly.** They encode years of accumulated knowledge about what wastes crawl budget, and several would improve the default reject patterns in [04](04-discovery-and-scoping.md).

### Heritrix

The Internet Archive's production crawler. Java, extremely capable, extremely heavy, XML-configured.

**Verdict:** not used. Wrong scale entirely.

### Zeno

The Internet Archive's newer Go crawler. WARC output, built for scale, single binary.

**Verdict:** worth watching. A single static binary with WARC output would be an appealing engine if it matures.

### HTTrack

The classic site mirrorer.

**Verdict:** not used. No WARC output, so no replay fidelity, no HTTP headers, no time dimension. It produces a rewritten copy, not an archive.

### wpull

wget-compatible Python crawler with WARC support, the engine underneath grab-site.

**Verdict:** not used directly, but it's the reference for how to build a crawler in Python if wget's limits become intolerable before the browser engine lands.

---

## Single-page capture

### SingleFile / single-file-cli

Browser extension and CLI producing one self-contained HTML file with everything inlined. Handles JS-rendered pages since it runs in a browser.

**Verdict:** good supplementary engine. Not WARC, so no pywb replay, but the resulting file is the most portable artifact that exists — it opens in any browser, forever, with no tooling.

### monolith

Same idea in Rust: fast, tiny, no browser. Doesn't execute JS, so static pages only.

**Verdict:** cheap supplementary engine. Nice for "keep a readable copy alongside the WARC."

### Obelisk

Go equivalent of monolith.

**Verdict:** noted, no advantage over monolith here.

---

## WARC infrastructure

### pywb

**Chosen for replay** ([D7](00-decisions.md#d7--replay-via-pywb-on-a-separate-origin)).

Serves WARC/WACZ collections with URL rewriting, framed replay, Memento, a CDX API, access controls, and — critically — one index across arbitrarily many WARC files. Also has a **recording mode** (`pywb` as a recording proxy), which is an interesting third capture path worth remembering.

The original notes reached pywb independently as the answer to "browse the whole site as one archive," and that's correct.

### warcio

Python WARC read/write library. Used for the raw record inspector and any WARC introspection.

### cdxj-indexer

Builds CDXJ indexes from WARC files. The core of the indexing post-processor.

### warcprox

MITM recording proxy — point any HTTP client through it and everything is WARC'd, including a headless browser or your own desktop browser.

**Verdict:** the universal escape hatch, and worth building eventually. Requires CA certificate distribution, which is the UX cost. It's also the cleanest way to record a browser session that a crawler can't reproduce.

### py-wacz / js-wacz

WACZ packaging. Used for export ([07](07-replay.md#wacz-export)).

### ReplayWeb.page / `<replay-web-page>`

Client-side WACZ replay via a service worker — no server at all. The `<replay-web-page>` custom element embeds it in any page.

**Verdict:** used for export/sharing, and a credible alternative primary replay path. pywb wins for large incrementally-growing archives because WACZ would need repackaging on every capture.

### warctools / warcat

WARC inspection and manipulation CLIs. Useful for debugging.

---

## Supporting libraries

| Library | Use | Why this one |
|---|---|---|
| `feedparser` | RSS/Atom parsing | Handles Atom's `<link href>` correctly — the exact gap that broke ArchiveBox here |
| `tldextract` | Registrable-domain grouping | Public Suffix List; naive suffix matching gets `co.uk` wrong |
| `selectolax` | HTML parsing | Very fast; discovery parses a lot of pages |
| `defusedxml` | XML parsing | Sitemaps are untrusted XML — entity-expansion attacks are live ([11](11-security.md#input-handling)) |
| `trafilatura` | Text extraction | Best-in-class boilerplate removal, for search indexing |
| `playwright` | Browser automation | Cookie minting, interactive profiles, browser engine |
| `argon2-cffi` | Password hashing | Argon2id |
| `cryptography` | Secret sealing | AES-GCM |
| `apscheduler` | Scheduling | SQLAlchemy job store, no extra service |
| `apprise` | Notifications | 80+ services, one dependency |

---

## Adjacent tools

| Tool | Relevance |
|---|---|
| [changedetection.io](https://changedetection.io/) | Page-change watching for feedless sites. Simple enough to build in |
| [FreshRSS](https://freshrss.org/) / [miniflux](https://miniflux.app/) | Import existing feed subscriptions rather than re-adding by hand |
| [Authelia](https://www.authelia.com/) / [Authentik](https://goauthentik.io/) | Second auth gate in front of an exposed instance |
| [Tailscale](https://tailscale.com/) | Avoid internet exposure entirely — the best security advice in this project |
| [restic](https://restic.net/) / [rclone](https://rclone.org/) | Offsite backup. WARCs are immutable and dedup well |
| [ntfy](https://ntfy.sh/) | Push notifications, self-hostable |
| [Gotenberg](https://gotenberg.dev/) | PDF rendering if PDF output is ever wanted |

---

## Summary: what this design actually uses

| Role | Tool | Milestone |
|---|---|---|
| Primary capture | GNU wget → WARC | M1 |
| Discovery | Custom (Python + selectolax + feedparser + defusedxml) | M2 |
| Indexing | cdxj-indexer | M3 |
| Replay | pywb | M3 |
| Record inspection | warcio | M3 |
| Cookie minting / interactive profiles | Playwright + Chromium | M5 |
| Feed parsing | feedparser | M6 |
| Second engine | browsertrix-crawler | M7 |
| Export / sharing | py-wacz + ReplayWeb.page | M8 |
| Media | yt-dlp | M8 |
| Text extraction | trafilatura | M8 |
| Notifications | Apprise | M6 |

Everything is standard, replaceable, and behind either the engine interface or a post-processor. The one hard dependency worth naming is pywb — replacing it would mean either client-side WACZ replay or writing a replay server, and only the first is realistic.
