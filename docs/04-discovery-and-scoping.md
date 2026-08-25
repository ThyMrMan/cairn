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

Platform fingerprinting happens here from the `generator` meta tag, response headers, and URL shape. Detecting Blogger, WordPress, Ghost, Substack, Squarespace, MediaWiki or Discourse enables a preset that supplies the right sitemap and feed paths, the right junk-parameter rejects, and the right asset hosts — which is most of the value of this phase.

Every platform the fingerprinter recognises must have a preset, and a test enforces it. Detection with nothing behind it is worse than no detection: the site reports a platform, offers no button, and shows the bare internal id where a name should be.

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

Every host discovered is grouped by **registrable domain** via `tldextract` (Public Suffix List). This matters more than it looks: `example.co.uk` and `other.co.uk` must not group as "uk", and `foo.blogspot.com` / `bar.blogspot.com` are *different sites* despite appearing to share a registrable domain.

**The PSL already knows this, via its private section.** An earlier version of this document planned to detect multi-tenant suffixes and fall back to full-host grouping. No such workaround is needed — `include_psl_private_domains=True` gives the right answer directly, because `blogspot.com` and `github.io` are listed as suffixes in their own right:

| host | default | with private domains |
|---|---|---|
| `foo.blogspot.com` | blogspot.com | **foo.blogspot.com** |
| `bar.blogspot.com` | blogspot.com | **bar.blogspot.com** |
| `1.bp.blogspot.com` | blogspot.com | **bp.blogspot.com** |
| `myblog.github.io` | github.io | **myblog.github.io** |
| `example.co.uk` | example.co.uk | example.co.uk |

Two people's blogs stay apart, and all four `N.bp.blogspot.com` image hosts group as one CDN — exactly what the picker should show. The bundled snapshot is used with `suffix_list_urls=()` so nothing fetches a list at runtime; a container that phones home on first use fails in precisely the air-gapped setups this tool is built for.

**A link to a file is not a link to a page.** Blogger wraps every post image in `<a href=".../s1600/image.jpg">` for its lightbox, so counting anchors naively gives the image CDN thousands of inbound "page" links and pushes it out of the assets-only default that is correct for it. Anchors whose URL carries an asset extension count as asset references instead.

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
  - resources.blogblog.com           # theme CSS/JS bundles; affects appearance
  - themes.googleusercontent.com     # skin background images (see the CSS-escape note above)
hosts_off:
  - www.blogger.com                  # auth CSS and comment-iframe JS: no archival value
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

  The host lists come from a real capture's gap report, not from guesswork.
  www.blogger.com is deliberately off: what a blog pulls from it is
  dyn-css/authorization.css and comment_from_post_iframe.js, which style the
  owner's admin bar and load a comment iframe that cannot work offline
  anyway. resources.blogblog.com is on, because that is where the theme's
  compiled CSS and JS live and their absence is visible.
```

The `?m=1` reject is worth calling out during development: without it, a Blogger crawl is roughly double the size and every page is stored twice with different URLs.

### Squarespace preset

```yaml
preset: squarespace
hosts_assets_on:
  - images.squarespace-cdn.com        # every uploaded image, every template
  - "*.squarespace-cdn.com"
  - static1.squarespace.com           # template CSS/JS and non-image uploads
  - assets.squarespace.com
  - fonts.googleapis.com              # the @font-face CSS …
  - fonts.gstatic.com                 # … and the font files: both halves needed
  - use.typekit.net
  - p.typekit.net
reject_patterns:
  - '[?&]format=json(-pretty)?(&|$)'  # the whole page again as JSON
sitemap_paths: ["/sitemap.xml"]
feed_paths: ["/blog?format=rss", "/news?format=rss", "/journal?format=rss"]
```

**No site-wide feed exists.** Each blog collection publishes its own at `<collection>?format=rss`, so there is no path that is right for every site — the three above are the usual collection names, tried after the page's own `<link rel="alternate">`, which is where a correct answer normally comes from.

**`?offset=` and `?tag=` are deliberately not rejected.** `?offset=<timestamp>` is the blog's own pagination and `?tag=` / `?category=` / `?author=` / `?month=` are real navigation. This is the Blogger lesson applied ahead of time: rejecting that platform's Older-posts trail saved nothing worth having and left a dead link at the bottom of every archived page. If a capture shows tag pages dominating, add the reject to that site rather than to the preset.

**`allow_extensionless` is off.** Squarespace image URLs keep the source extension ahead of the query (`…/photo.jpg?format=2500w`) and the asset pattern already permits an extension followed by `?`, so they match without it. Turning it on would let a crawl follow HTML on the CDN for no gain.

> **Unlike Blogger's, this preset's reject list has not been measured against a real capture.** The hosts and paths are Squarespace's documented infrastructure and are safe; the single reject is the structural twin of WordPress's `/wp-json/`. A capture's "what it fetched" list is what would turn it into a preset that pulls its weight — that is how Blogger's twelve patterns were arrived at, and five of them were added only after a browser-engine capture showed them to be half of all requests.

### MediaWiki preset

A wiki is the platform where a preset earns the most, because two of its per-article views are unbounded rather than merely repetitive.

| Rejected | Why |
|---|---|
| `[?&]diff=` | An article with N revisions offers on the order of **N²** diffs |
| `[?&]oldid=` | One URL per revision per article |
| `Special:Random` | A crawl with no end condition at all |
| `action=edit\|history\|info\|purge\|…` | A fixed set of views on **every** article |
| `[?&]uselang=` / `[?&]useskin=` | The entire wiki again, per language and per skin |
| `[?&]printable=yes`, `mobileaction=`, `redirect=no`, `curid=` | Duplicate views of a page reached another way |
| `/api.php` | The machine-readable twin of everything |
| `Special:RecentChanges\|Search\|Export\|WhatLinksHere\|…` | Live queries and sign-in forms |

**`action=raw` is deliberately not rejected**, though it is the wikitext twin of every page and looks like the most obvious member of that list. Wikis predating `load.php` — and gadgets on wikis that do not — load site CSS through `MediaWiki:Common.css?action=raw&ctype=text/css`. Rejecting it costs the wiki's entire custom appearance; keeping it costs one extra fetch per article. Same asymmetry the asset extension list is deliberately generous for.

**`Special:AllPages` and the other index pages are left alone** — they are how a wiki is enumerated when it publishes no sitemap.

**Namespaces are left entirely alone.** `Talk:`, `User:`, `File:`, `Template:` and `Category:` are all crawled, because on many wikis the talk pages are the most valuable content and on others they are noise. That is a per-site decision, not a preset's.

**No feed.** MediaWiki's only feed is `Special:RecentChanges` in Atom form, and its entries link to *diffs* rather than articles — so watching it would report new items on every poll and archive none of them, since the `diff=` reject puts every one out of scope. Watch `/sitemap.xml` instead where a wiki publishes one.

### Discourse preset

| Rejected | Why |
|---|---|
| `/message-bus/` | The live-update long-poll. Under a browser engine it does not stop — the page keeps re-opening it |
| `/session/`, `/admin/`, `/logs/` | Auth and moderation, which cannot work offline |
| `/u/<name>/(activity\|notifications\|preferences\|…)` | One set per member, none of it forum content |
| `/search?` | Generated on demand, endless |
| `[?&](order\|ascending)=` | Sort permutations of lists you already have |
| `[?&]_=<digits>` | Cache-busting timestamps: a new URL for the same asset |

Two much larger savings are **left switched off**, because both cost something:

**Post-number URLs.** Discourse addresses a topic as `/t/slug/123` and any position within it as `/t/slug/123/47` — the same topic page, so a 500-post thread can be crawled as 500 near-identical URLs. Rejecting `/t/[^/]+/[0-9]+/[0-9]+$` collapses that, at the price of deep links into a thread going dead in replay.

**The `.json` twins.** Nearly every Discourse URL answers with JSON at the same path plus `.json`, which doubles the crawl — but **which way to trade depends on the engine**. Fetched with wget you get Discourse's server-rendered crawler HTML and the JSON is redundant; a browser engine captures the JavaScript app, which reads those endpoints to render anything at all. Reject `\.json($|\?)` on a wget capture, leave it alone on a browsertrix one.

> Neither of these two has been measured against a real capture either. The patterns come from MediaWiki's and Discourse's documented URL structures. What is verified is that every pattern in every preset compiles under **both** regex engines — PCRE for wget, JavaScript for browsertrix — and matches the same URLs in each, and that no preset rejects the feed or sitemap path it goes looking for.

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
  "global_reject_patterns": ["[?&]utm_[a-z]+="],
  "max_depth": null,
  "max_pages": null,
  "max_bytes": 21474836480,
  "obey_robots": true,
  "politeness": {"wait_s": 1.0, "random_wait": true, "rate_limit": "2m", "concurrency": 1}
}
```

### The skip list that applies to every site

`reject_patterns` is this site's own — typed here, or contributed by its preset. `global_reject_patterns` is the instance-wide list from **Settings → Skip these URLs everywhere**, and it is a different thing in one specific way: it is **merged as the scope is resolved rather than stored on the site**.

That is the whole design, and the alternative is what makes it worth stating. If the list were copied into each site when the site was created:

- adding a pattern would reach only sites created afterwards, so the rule would arrive by the calendar;
- removing one would mean "stop giving it to new sites", leaving it behind in every site that already had it, indistinguishable from a pattern somebody typed there on purpose.

Merging at resolve time gives the opposite of both: one list, retroactive in both directions, and a site's own patterns still readable as a site's own. The two never mix — `GET /sites/{id}/scope` returns them in separate fields precisely because the domain picker posts back what it was given, and returning them merged would copy the global list into the site on the first save.

Both lists mean the same thing wherever a reject pattern is read, so a global pattern also decides what replay serves (`replay.withheld_patterns`) and what the scope preview counts. Nothing is deleted from disk: a pattern added today stops the next capture fetching those URLs and hides already-archived ones from the index; removing it brings them back on the next rebuild.

**A site can excuse itself from an individual pattern** — `global_reject_exceptions`, edited in that site's own domain picker, stored in `scope_settings`. Without it the list would be a one-way door: a rule that is right for the web in general and wrong for one blog could only be dealt with by deleting it for everybody. It is the same trap `retired_patterns` was added to presets to get out of.

Exceptions are matched by the pattern's text, so editing a global pattern retires the exceptions granted to its predecessor and every site starts obeying the new rule. They are *not* dropped when the pattern is removed from the list — otherwise turning a global rule off and on again would silently re-apply it to the sites that had opted out.

Invalid regexes are refused at write time rather than at capture time. A bad pattern in one site's list breaks that site; a bad pattern here breaks every capture on the instance at once, so it is caught while somebody can still see what they typed.

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
| `reject_patterns` + `global_reject_patterns` | `--reject-regex` |
| `max_depth` (null) | `--level=inf` |
| `max_bytes` | `--quota=` |
| `obey_robots: false` | `-e robots=off` |
| `politeness` | `--wait`, `--random-wait`, `--limit-rate`, `--tries`, `--waitretry` |

**The `crawl_pages: false` translation is the awkward one.** wget's `--domains` is a single flat allowlist with no notion of "assets only" — anything in `--domains` can be recursed into. The reliable approach is a generated reject regex that blocks non-asset paths on asset-only hosts:

```
--reject-regex='^https?://(1|2|3|4)\.bp\.blogspot\.com/(?!.*\.(jpe?g|png|gif|webp|svg|css|js|woff2?)($|\?))'
```

`--reject-regex` uses POSIX ERE by default, which has **no lookahead** — the pattern above fails with *"Invalid preceding regular expression"*. Pass `--regex-type=pcre`.

**Verified on Debian's wget 1.25.0 (the container base):** `--regex-type=pcre` works and honours lookahead correctly. Note that the version banner reports **neither** `+pcre` nor `-pcre` — that flag only ever described PCRE1, while Debian links PCRE2 and doesn't advertise it. Grepping the banner therefore rejects a perfectly good wget; the image's build-time check compiles an actual lookahead pattern instead ([10](10-deployment-unraid.md#dockerfile-sketch)).

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
- `allow_extensionless: true` — set by the Blogger preset for `blogger.googleusercontent.com`, `lh3.googleusercontent.com` and `themes.googleusercontent.com`, which serve images through extension-less URLs and do not serve linked HTML.

  `themes.googleusercontent.com` was missing from that list until a live capture exposed it, and it is the instructive case: **every** URL on that host is `image?id=…`, so the flag is not an edge case there, it is the whole host. Listing a host under `assets_on` without also listing it under `extensionless_ok` puts it inside `--domains` and then rejects every URL it serves — in scope, and reachable by nothing. When adding a host to a preset, check what its URLs actually look like before assuming the default is safe.

### Assets the crawler cannot reach at all

Some references never become a URL the crawler can see. A Blogger skin writes its theme images as `url(https\:\/\/themes.googleusercontent.com\/image?id=…)`; wget does not decode CSS escapes, so it requests the escaped text against the blog itself, 404s, and never learns the real URL exists. No scope setting reaches these — the host can be perfectly in scope and the asset is still lost.

Discovery already decodes them, because its extractor has to agree with the audit's. So it records that subset separately (`escaped_assets`) and the capture injects them into the seed file alongside the sitemap and feed URLs, filtered by `fetch_assets` rather than `crawl_pages` — they are images on hosts nobody wants crawled as websites.

This is the general shape for a whole class of problem: **where discovery can see something the crawler cannot, hand it over as a seed rather than hoping the crawler finds it.** Lazy-loaded images are the same shape and are not solved this way, because a `data-src` attribute is not necessarily a URL the server will serve — that one needs a browser engine.

### An exclusion is not a gap

The audit splits what a page asked for and did not get into two lists that read very differently:

- **In scope and still absent** — `missing_assets`. Something went wrong, or a flag is set wrong. This is the number worth acting on.
- **Outside the scope** — `excluded_assets`. A host with its boxes unticked, or a URL a reject pattern covers. This is a setting, and the report says so and names the host.

Keeping them in one number is what makes a report worth ignoring. On a Blogger blog the second list is never empty — the preset deliberately drops `www.blogger.com`, whose contribution is the owner's admin-bar CSS and a comment iframe that cannot work offline — so every capture would open with "3 referenced assets were not captured" forever. Three rounds of live testing were spent chasing exactly that, while it was working as designed.

Classification needs positive evidence that somebody chose the exclusion. A scope that will not parse, or that carries no asset hosts at all, reports everything as absent — under-explaining is recoverable, explaining away a real gap is not.

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

### Why the crawl is always bigger than this number

**Pages to crawl** counts pages, on crawlable hosts, after rejects. The live counter during a capture counts **every URL the engine fetches** — images, stylesheets, fonts, everything. On a photo blog three or four assets per post is ordinary, so a crawl running at 3–4× the preview is not a fault, and a progress bar drawn from one against the other would be nonsense.

It is also an extrapolation rather than an enumeration: the preview scales the surviving fraction of discovery's sample by `urls_found / len(sample)`, and discovery itself crawls at most `max_pages` (100 by default) to depth 3.

So the ratio is the signal, not the difference. Somewhere past **10×** the crawl has usually found a corner of the site it can generate forever. Real example, on a Blogger blog whose index found 38,000 posts and whose crawl passed 140,000 URLs:

```
Shape                                    Count      %
/search/label/*?max-results&updated-max  148,893   ~⅓
```

Label archives paginate through `updated-max`, and every label page links every other label — combinatorial, and it does not terminate in any useful time. The posts were coming from the sitemap regardless; the crawl was spending its life on the pagination.

**`What it fetched`**, on each capture, is the report that answers this without guessing. It groups the capture's URLs by shape — varying path segments replaced, the query reduced to its key names — so a hundred thousand label URLs become one row with a count. It works while the crawl is still running, which is when it matters. Grouping is by cardinality *within a prefix*: nothing about `/search/label/Travel` in isolation says the last segment is a value, and only the four hundred siblings say so.

#### A shape is not a pattern

This report writes `#` for a numeric segment, `*` for a varying one and `?a&b` for the query keys. That is **its own shorthand, not a regular expression**, and the two sit two panels apart with the same URLs in them — so the shape gets copied into the skip box, where `#` is a literal that no fetched URL contains, because fragments are stripped before the request.

Nothing catches it. The pattern compiles, saves, and matches zero URLs; the list shows it as applied. It was found by counting a crawl an hour later and seeing `/feeds/#/comments/default` still at 35% of all fetches, with the rule that was supposed to stop it sitting in the settings above.

Two things follow from that, and both are about closing the gap rather than documenting it.

**Each row can be turned into a pattern.** `Skip` on the row generates the regex from the notation that produced it — `#` → `[0-9]+`, `*` → `[^/?]+` with any extension kept, literals escaped, query keys as order-independent lookaheads because a shape sorts them and a URL does not. It is anchored at the path root and closed at the end, so `/feeds/#/comments` cannot match halfway through a longer path. Only characters special in *both* PCRE and JavaScript are escaped: `re.escape` emits `\-` and `\&`, which JavaScript rejects under the `u` flag, and both engines have to compile the same reject set ([05](05-capture-engines.md)).

**The generated pattern is checked against the row's own example before it is offered**, and withheld if it misses. A generated pattern that does not match the URL it was generated from is the same silent no-op, and offering one would be the bug with a button on it.

#### What a pattern matches

Every skip pattern, however it was written, is counted against the URLs recent captures actually fetched — `capture_urls` already holds them. A pattern matching nothing says **matches nothing** beside it instead of looking identical to one doing all the work.

The count is against **what was fetched, not what will be**. A pattern that fires stops those URLs being discovered at all, so the next capture's list is smaller than the count predicts. It is a floor and a sanity check, which is what *did I write this right?* needs; simulating the next crawl is a different and far more expensive question, and pretending otherwise would put a precise-looking number on a guess.

The sample is bounded — 20,000 URLs from the 12 most recent captures — because it runs while somebody waits for a panel to draw. A truncated count is shown as `8,259+` rather than silently as a total.

**The stop switch is `max_pages`, and it counts URLs rather than pages** despite the name — the supervisor counts every `url` event, assets included. Set it as though it meant pages and the crawl stops at roughly a quarter of the site. The scope editor labels it in URLs for that reason.

---

## Re-running discovery

Discovery on an established site diffs against the previous run and surfaces:

- **New hosts** — the blog started embedding a new CDN or comment system
- **Disappeared hosts** — an asset host went away; existing captures may reference dead resources
- **New URLs not in any capture** — a backfill gap
- **URLs captured but no longer in the sitemap** — deleted posts, which are now *only* in your archive

That last category is the one worth a notification. It's the exact moment archiving justified itself, and it's also a signal to protect that capture from any retention policy that might prune it.

Schedule discovery independently of capture (monthly is a reasonable default) so scope drift gets noticed without paying for a full recapture.

---

## Discovery through a browser

Everything above reads HTML as the server sent it, which is fast, needs no
dependencies, and is correct about everything a server sends. It is also
structurally blind to anything a page decides at runtime, and there are three
of those. Measured against a fixture built to expose each one:

| | fetch | render |
|---|---|---|
| a host named only inside a script | not found | found |
| a page behind a link the script appends | never sampled | sampled |
| a page behind infinite scroll | never sampled | sampled |

So discovery has a **browser mode**, off by default, reusing the Chromium that
arrived in M5. It renders only the *sampling* phase: robots.txt, sitemaps and
feeds are XML and text that no script rewrites, and putting a browser in front
of them costs seconds per document to find nothing.

**The network log is the evidence, not the rendered DOM.** This is the
correction, and it matters because the obvious implementation — render the page
and re-parse it — does not work. `new Image().src = "//cdn/pixel.gif"` fetches
without ever entering the document, so the DOM re-parse misses precisely the
host the feature exists to find. Measured on the fixture: the rendered DOM
yielded two asset hosts, the browser's own request log yielded three, and the
missing one was the JavaScript-only pixel. The log also carries each response's
real content type, which is better data than the fetch path has — it only
learns a MIME type for pages it fetched itself.

**A run says whether it was worth it.** Every host the browser requested is
compared against every host some page's *served* HTML named, and the difference
is reported: either "3 host(s) were found only by rendering — nothing in the
HTML names them" or "rendering 12 page(s) found no host the HTML did not
already name. This site does not need the browser for discovery." The second
sentence is the one that saves the next hour.

Rendering is capped at 40 pages regardless of what was asked for. A browser
takes seconds per page where a fetch takes milliseconds, and the sample only
has to be big enough to see every host the template uses.

---

## Sites that span domains

A blog that moved to a custom domain, or one that lives at two, is **one site
with several seeds** — one scope, one index, one replay collection. Splitting
it into two sites gives two half-histories and a capture selector that lies
about which versions of a page exist.

Seeds after the first live in `sites.scope_settings`, not a table of their own:
a seed is a scope decision, that is where the scope's non-per-host decisions
already live, and a `site_seeds` table would be a migration and a join for a
list that is one entry long on almost every site.

Three things follow from adding a seed, and each was a bug before it was a
rule:

1. **Its host becomes crawlable.** A seed the scope would refuse on its first
   request reads in the capture report exactly like a site that is down.
2. **Its origin is enumerated separately.** Each domain has its own robots.txt
   and its own sitemap; enumerating the second against the first one's map of
   itself would archive a site's new home from a list of its old one's pages.
3. **It is sampled from scratch rather than reached by link-following.** A site
   that migrated has a second landing page that nothing on the first one links
   to, so waiting to arrive there means never arriving.

**Saving the domain picker must not delete them.** The picker submits hosts and
patterns and no seeds at all, so a wholesale rewrite of `scope_settings` drops
everything after the first address the moment anybody ticks a checkbox. It also
drops `user_edited`, which is what stops the next re-index overwriting a
hand-picked scope. `save_scope` therefore merges onto what is stored rather
than replacing it, and seeds are changed only through their own endpoints.
