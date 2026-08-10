# 04 — Discovery & Scoping

Covers R5 (initial index producing an organized domain list) and R6 (selecting domains in the UI), plus how that selection becomes an enforced crawl boundary.

The ArchiveBox evaluation's issues 1, 2, and 4 all live here: depth limits that can't reach paginated content, hop-following that ignores domain boundaries, and sitemaps whose `<loc>` URLs never get extracted. Discovery is designed so none of those failure modes are reachable — URLs come from authoritative sources rather than from chasing links, and scope is a hard boundary rather than a side effect of depth.

---

## The discovery job

A separate job type ([D10](00-decisions.md#d10--discovery-is-a-separate-cheap-re-runnable-job)) that writes no WARCs, finishes in seconds to a couple of minutes, and can be re-run any time.

### Phase 1 — Probe the origin

```
GET  /robots.txt          → Sitemap: directives, Disallow rules (recorded, not obeyed yet)
GET  <seed URL>           → <link rel="alternate">, <link rel="canonical">, generator meta,
                            pagination rel=next/prev, base href
HEAD /sitemap.xml
HEAD /sitemap_index.xml
HEAD /feed  /rss  /atom.xml  /index.xml  /feeds/posts/default
```

Platform fingerprinting happens here from the `generator` meta tag, response headers, and URL shape. Detecting Blogger, WordPress, Ghost, Substack, or Squarespace enables a preset that supplies the right sitemap and feed paths, the right junk-parameter rejects, and the right asset hosts — which is most of the value of this phase.

### Phase 2 — Enumerate URLs from authoritative sources

Preferred over crawling, because it's complete and cheap:

**Sitemaps.** Parse `<loc>` from `<urlset>`; recurse into `<sitemapindex>` children (bounded, with a visited set — sitemap indexes can be circular). Follow Blogger's pagination convention `?page=2`, `?page=3`, … until an empty result, since Blogger caps each sitemap file at ~150 URLs. Record `<lastmod>` where present — it's what later powers "only recapture changed pages."

**Feeds.** Parse with `feedparser`. Walk paginated feeds via RFC 5005 `rel="next"`, and for Blogger via `start-index` / `max-results`:

```
/feeds/posts/default?start-index=1&max-results=500
/feeds/posts/default?start-index=501&max-results=500
```

Blogger silently caps `max-results` at 500, so request 500 and paginate on the returned count rather than trusting the parameter. Both Atom (default) and RSS (`&alt=rss`) work — `feedparser` handles Atom's `<link href>` form correctly, which is the ArchiveBox gap from issue 3.

**Archive/label pages.** Blogger's `/search/label/<x>` and `/<year>/<month>/` listings surface posts that predate the feed window. Note that `robots.txt` on Blogger disallows `/search`, so reaching label pages requires the robots override — surfaced in the UI as an explicit toggle with that explanation, not buried in advanced settings.

### Phase 3 — Bounded sampling crawl

Only after the above, and only to find things the sitemap doesn't list — mainly *which hosts serve subresources*.

```yaml
max_pages: 100          # hard cap, default
max_depth: 3
same_host_only: true    # link-following during discovery never leaves the seed host
timeout_s: 60
concurrency: 2
```

Each sampled page is parsed for:

| Extracted | From |
|---|---|
| Page links | `<a href>` |
| Subresources | `<img src\|srcset>`, `<script src>`, `<link href>` (stylesheet/preload/icon), `<video>`, `<audio>`, `<source>`, `<iframe src>`, `<embed>`, `<object data>` |
| CSS references | `url(...)` and `@import` inside `<style>` and fetched stylesheets |
| Lazy-load hints | `data-src`, `data-srcset`, `data-original`, `data-lazy-src` |
| Feeds | `<link rel="alternate" type="application/rss+xml\|atom+xml">` |

Discovery counts hosts; it does not follow them off-host. Sampling a hundred pages is enough to see every asset host a template uses.

**What discovery cannot see.** Hosts referenced only from JavaScript, and content behind infinite scroll. If the platform fingerprint suggests a JS-heavy site, or the sampling crawl finds very few subresource hosts relative to page count, the UI should say so and offer a browser-based discovery pass (M7, once a browser engine exists) rather than silently under-reporting.

---

## The domain picker

Every host discovered is grouped by **registrable domain** via `tldextract` (Public Suffix List). This matters more than it looks: `example.co.uk` and `other.co.uk` must not group as "uk", and `foo.blogspot.com` / `bar.blogspot.com` are *different sites* despite sharing a registrable domain — so `blogspot.com`-style multi-tenant suffixes get flagged and grouping falls back to full host.

### Classification

| Signal | Meaning |
|---|---|
| `link_refs > 0, asset_refs = 0` | A site you might crawl |
| `asset_refs > 0, link_refs = 0` | Pure asset host — fetch, don't crawl |
| both > 0 | Mixed; usually the seed host or a sibling |
| Matches known-analytics list | Suggest excluding entirely |

Role guessing uses hostname patterns plus observed MIME types: `images` (majority image responses), `cdn` (mixed static), `fonts` (`fonts.g*`, woff), `analytics` (a maintained blocklist — Google Analytics, GTM, doubleclick, Facebook pixel, Hotjar, …), `social` (share widgets), `comments` (Disqus, Blogger comment iframes).

### What the table shows

| Host | Registrable | Links | Assets | URLs | Role | Crawl | Assets |
|---|---|---:|---:|---:|---|:-:|:-:|
| `example.blogspot.com` | blogspot.com | 1,834 | 210 | 1,847 | self | ☑ | ☑ |
| `1.bp.blogspot.com` | blogspot.com | 0 | 3,201 | 2,890 | images | ☐ | ☑ |
| `blogger.googleusercontent.com` | googleusercontent.com | 0 | 1,455 | 1,402 | images | ☐ | ☑ |
| `www.blogger.com` | blogger.com | 42 | 18 | 12 | social | ☐ | ☐ |
| `fonts.gstatic.com` | gstatic.com | 0 | 96 | 6 | fonts | ☐ | ☑ |
| `otherblog.blogspot.com` | blogspot.com | 87 | 0 | 87 | unknown | ☐ | ☐ |
| `www.google-analytics.com` | google-analytics.com | 0 | 100 | 1 | analytics | ☐ | ☐ |

Two independent checkboxes per host — **Crawl** (follow this host's page links) and **Assets** (allow subresources from it) — is the core of the UI. Hovering a host expands sample URLs. Bulk actions: select all images/CDN, deselect all analytics, invert.

### Defaults

Preselect so the common case needs zero clicks:

- Seed host → Crawl ✓, Assets ✓
- Any host with `asset_refs > 0` and `link_refs == 0` and not classified analytics → Assets ✓
- Analytics/ads → both ✗
- Everything else → both ✗, listed for review

That last line is the important one. Defaulting unknown hosts to *off* means the crawl can never wander onto `otherblog.blogspot.com` because it was in the sidebar — ArchiveBox's issue 2, made structurally impossible instead of patched with a regex allowlist after the fact.

### Blogger preset

Applied when the fingerprint matches:

```yaml
preset: blogger
hosts_assets_on:
  - "*.bp.blogspot.com"              # 1.bp, 2.bp, 3.bp, 4.bp — image CDN
  - blogger.googleusercontent.com
  - lh3.googleusercontent.com
  - "*.ggpht.com"
  - fonts.gstatic.com
hosts_off:
  - www.blogger.com
  - "*.google-analytics.com"
  - "*.doubleclick.net"
reject_patterns:
  - '[?&]m=1'                        # mobile duplicate of every page
  - '[?&]replytocom='                # comment-reply permutations
  - '[?&]showComment='
  - '/search\?updated-(max|min)='    # infinite archive pagination loops
  - '\?action=backlinks'
seeds_from: [sitemap, feed]
notes: |
  Blogger serves every post twice (?m=1 mobile). Rejecting it halves the crawl
  with no content loss. /search is robots-disallowed but is where label pages
  live — enable the robots override if you want them.
```

The `?m=1` reject is worth calling out during development: without it, a Blogger crawl is roughly double the size and every page is stored twice with different URLs.

---

## From selection to scope

Selections become a **resolved scope object** — engine-independent, stored on the site, passed verbatim into every job spec, and recorded in `manifest.json` so a capture's boundary is auditable later.

```json
{
  "seeds": ["https://example.blogspot.com/"],
  "seed_urls_from": {"sitemap": true, "feeds": true},
  "hosts": [
    {"host": "example.blogspot.com", "crawl_pages": true, "fetch_assets": true},
    {"host": "1.bp.blogspot.com", "crawl_pages": false, "fetch_assets": true},
    {"host": "blogger.googleusercontent.com", "crawl_pages": false, "fetch_assets": true}
  ],
  "path_prefix": null,
  "accept_patterns": [],
  "reject_patterns": ["[?&]m=1", "[?&]replytocom="],
  "max_depth": null,
  "max_pages": null,
  "max_bytes": 21474836480,
  "obey_robots": true,
  "politeness": {"wait_s": 1.0, "random_wait": true, "rate_limit": "2m", "concurrency": 1}
}
```

### Translation to wget

| Scope field | wget |
|---|---|
| `hosts` with any entry | `--span-hosts` |
| all hosts | `--domains=a,b,c` |
| explicitly excluded hosts | `--exclude-domains=…` |
| `crawl_pages: false` hosts | `--reject-regex` on those hosts' *page* URLs; `-p` still pulls their assets |
| `fetch_assets` | `--page-requisites` |
| `path_prefix` | `--no-parent` + seed at that path, or `--include-directories` |
| `accept_patterns` | `--accept-regex` |
| `reject_patterns` | `--reject-regex` |
| `max_depth` (null) | `--level=inf` |
| `max_bytes` | `--quota=` |
| `obey_robots: false` | `-e robots=off` |
| `politeness` | `--wait`, `--random-wait`, `--limit-rate`, `--tries`, `--waitretry` |

**The `crawl_pages: false` translation is the awkward one.** wget's `--domains` is a single flat allowlist with no notion of "assets only" — anything in `--domains` can be recursed into. The reliable approach is a generated reject regex that blocks non-asset paths on asset-only hosts:

```
--reject-regex='^https?://(1|2|3|4)\.bp\.blogspot\.com/(?!.*\.(jpe?g|png|gif|webp|svg|css|js|woff2?)($|\?))'
```

`--reject-regex` uses POSIX ERE by default, which has **no lookahead** — the pattern above fails with *"Invalid preceding regular expression"*. Pass `--regex-type=pcre`.

**Verified on Debian's wget 1.25.0 (the container base):** `--regex-type=pcre` works and honours lookahead correctly. Note that the version banner reports **neither** `+pcre` nor `-pcre` — that flag only ever described PCRE1, while Debian links PCRE2 and doesn't advertise it. Grepping the banner therefore rejects a perfectly good wget; the image's build-time check compiles an actual lookahead pattern instead ([10](10-deployment-unraid.md#build)).

### What running it actually established

Three findings from putting this translation in front of wget 1.25.0 with a two-host fixture. All three constrain what the engine may generate, and the first two were open questions this document previously got wrong by omission.

**1. The translation works.** A reject regex that blocks page URLs on an assets-only host still lets `--page-requisites` fetch that host's images, CSS and fonts, including requisites discovered two levels into the crawl. This was M1's flagged risk.

**2. `--domains` is a hard gate, and `--page-requisites` does not bypass it.** Omitting an asset host from `--domains` — the obvious "just don't allow it" reading — drops its assets entirely rather than fetching them as requisites. So an assets-only host must be **both** listed in `--domains` and fenced by the reject regex. There is no regex-free formulation, and the fallback below is the only alternative shape.

**3. No regex over URLs can distinguish an extension-less image from an extension-less page.** The URL text is identical. That makes the allowlist above silently drop Blogger's proxied images:

```
https://lh3.googleusercontent.com/blogger_img_proxy/AEn0k_...   ← no extension, is an image
https://lh3.googleusercontent.com/about                         ← no extension, is a page
```

Both readings cost something. Rejecting extension-less URLs loses images with no error anywhere; accepting them lets wget crawl a CDN's HTML, which is the failure this whole design exists to prevent. Neither default is right for every host, so it is a per-host decision:

- `allow_extensionless: false` (default) — safe, and the scope preview says plainly that extension-less URLs on that host will be skipped.
- `allow_extensionless: true` — set by the Blogger preset for `blogger.googleusercontent.com` and `lh3.googleusercontent.com`, which serve images through extension-less URLs and do not serve linked HTML.

Because neither setting is reliably correct, the `asset-audit` post-processor closes the loop from the other end: after a capture it reads the archived HTML back out of the WARC and reports assets a page referenced but the crawl never fetched. That catches a dropped image regardless of which way the flag was set, and catches lazy-loaded images too — which no scope setting can reach, since wget does not execute JavaScript.

If you ever hit a wget that genuinely lacks PCRE, the fallback is to invert the logic into an `--accept-regex` that positively lists asset extensions on those hosts. Encode whichever you use in the engine, because getting it wrong means either crawling image CDNs as websites or dropping images entirely.

### Scope preview

Before a capture runs, show a dry-run summary computed from discovery data — no fetching:

```
Scope preview
  Pages to crawl        ~1,847  (example.blogspot.com)
  Asset hosts allowed        4
  Excluded by pattern      912  (?m=1 duplicates)
  Estimated size        ~3.2 GB  (from sampled page sizes)
  Estimated time         ~1h 40m  (at 1 req/s)
```

Rough numbers, but they catch the two mistakes that waste hours: a scope that accidentally includes a neighboring blog, and a missing `?m=1` reject.

---

## Re-running discovery

Discovery on an established site diffs against the previous run and surfaces:

- **New hosts** — the blog started embedding a new CDN or comment system
- **Disappeared hosts** — an asset host went away; existing captures may reference dead resources
- **New URLs not in any capture** — a backfill gap
- **URLs captured but no longer in the sitemap** — deleted posts, which are now *only* in your archive

That last category is the one worth a notification. It's the exact moment archiving justified itself, and it's also a signal to protect that capture from any retention policy that might prune it.

Schedule discovery independently of capture (monthly is a reasonable default) so scope drift gets noticed without paying for a full recapture.
