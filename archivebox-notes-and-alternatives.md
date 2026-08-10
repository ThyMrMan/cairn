# Website Backup Tooling — ArchiveBox Notes & Alternatives

Reference notes from evaluating ArchiveBox for backing up Blogger-hosted sites, plus alternative tools researched against the same requirements.

## Original requirements

- Runs on Unraid or via Docker
- Can import cookies or Tampermonkey-style scripts to get past a Blogger content-warning interstitial
- Can watch RSS feeds / a site and auto-backup new content as it's published
- Lets you sort and organize backed-up sites by folder or tag

---

## ArchiveBox: issues found and workarounds

ArchiveBox met the requirements on paper, but several behaviors weren't obvious going in. Notes below are roughly in the order they came up.

### 1. `--depth` is not a full-site crawl

`archivebox add` (and `archivebox schedule`) only support `--depth=0` or `--depth=1` — there's no `--depth=2`, `--depth=3`, etc.

- `--depth=0`: archives only the exact URL you gave it.
- `--depth=1`: archives that URL *plus* every link found one hop out from it (the outlinks on that one page).

If a site's pagination is sequential (homepage → page2 → page3, where the homepage only links to page2), `--depth=1` will not reach page3 — it's two hops from the seed URL and ArchiveBox has no deeper setting to chase it.

**Workaround:** don't rely on hop-following for a full backfill. Feed ArchiveBox a complete list of URLs directly (see sitemap/RSS section below) so there's no crawling required.

### 2. Depth-following doesn't respect domain boundaries

`--depth=1` will queue *any* link it finds one hop out, including sidebar links to unrelated domains (e.g. `domainB.com` linked from `domainA.com`'s homepage).

**Workaround:** set a regex allowlist (and optionally a denylist) so out-of-scope URLs get silently skipped regardless of depth:

```
archivebox config --set URL_ALLOWLIST='^https?://(www\.)?domainA\.com/'
archivebox config --set URL_DENYLIST='<optional regex for paths to exclude>'
```

Denylist takes precedence when both are set. This also incidentally filters out XML namespace/schema URLs that show up in sitemap or feed files (see issue 4).

### 3. RSS/Atom feed parsing is unreliable for Atom-format feeds

ArchiveBox's dedicated feed parser is an older regex-based one. There's an open GitHub issue (#1171) noting it "cannot be parsed" for Atom-style `<link href="...">` tags — it was built around RSS 2.0's plain `<link>...</link>` text form. Blogger's default feed format is Atom.

**Workarounds (pick one):**
- Force Blogger to serve RSS 2.0 instead of Atom by appending `&alt=rss` to the feed URL:
  `https://domainA.blogspot.com/feeds/posts/default?max-results=25&alt=rss`
- Or rely on ArchiveBox's generic text/URL-regex fallback parser (`generic_txt`), which it falls back to when a dedicated parser doesn't recognize the format — test it first and confirm in the UI that you get one Snapshot per post, not a single Snapshot of the feed page itself.

### 4. Sitemap.xml links weren't extracted at all

Loading `sitemap.xml` at `--depth=0` just archived the sitemap page itself (one WARC of the raw XML) — none of the individual post URLs inside it got queued. This is expected once you know how depth works (see issue 1): a fetched URL is treated as a normal page, and its outlinks are only followed at `--depth=1`.

Retrying with `--depth=1` is the first thing to try, but it comes with a caveat: ArchiveBox's outlink-following appears to be built around parsing standard HTML `<a href="...">` links. A sitemap.xml is XML, and its URLs live inside `<loc>...</loc>` tags, not anchor tags — it's unconfirmed whether depth-based crawling picks those up.

**Reliable fallback (bypasses ArchiveBox's parsing entirely):** extract the URLs yourself and import them as a plain text list, which is the one input format every ArchiveBox version handles unambiguously:

```bash
curl -s 'https://domainA.blogspot.com/sitemap.xml' | grep -oP '(?<=<loc>)[^<]+' > domainA_urls.txt
archivebox add --depth=0 < domainA_urls.txt
```

If the sitemap is paginated (Blogger caps each file at ~150 URLs), repeat against `sitemap.xml?page=2`, `?page=3`, etc. and append to the same file. Keep `--depth=0` here since the file already contains the exact final URLs.

The same fallback approach is more dependable for the *ongoing* RSS watch too — run the curl/grep extraction on a cron schedule (or Unraid User Scripts) and pipe into `archivebox add`, rather than trusting `archivebox schedule` to correctly parse whatever XML dialect the feed happens to be in.

### 5. One WARC file per page — by design, not a bug

ArchiveBox creates one Snapshot per URL, each in its own `archive/<timestamp>/` folder with its own WARC, screenshot, singlefile copy, etc. Backfilling a blog with hundreds of posts means hundreds of folders — this is inherent to ArchiveBox's data model, not something to "fix" via a setting. There's no built-in way to combine snapshots into one shared WARC.

### 6. No built-in way to browse the whole site as one archive

Because each page is a separate WARC, there's no ArchiveBox feature to browse the backfilled site as a single cohesive unit.

**Solution: index, don't merge.** A WARC replay tool (e.g. [pywb](https://pywb.readthedocs.io/), which ArchiveBox's own docker-compose examples already reference) can build one combined index (CDXJ) across many separate WARC files, mapping every URL to the correct file + byte offset. You then browse the entire site through pywb's single URL bar — same principle the Wayback Machine itself uses internally (huge numbers of small WARCs, one combined index). No need to touch or reorganize the underlying files.

```
pip install cdxj-indexer
cdxj-indexer <path-to-archivebox-data>/archive > combined.cdxj
# then serve via pywb / wayback pointed at the same WARC directory + this index
```

Note: physically concatenating WARC.gz files with `cat` *is* technically valid (each record is an independently-gzipped member), but any index still has to be built against wherever the bytes end up — so concatenating first doesn't save the indexing step. Simpler to just index the files in place.

### 7. No way to organize the archive folder by tag on disk

Tags in ArchiveBox are database-only metadata (drive filtering in the web UI/CLI) — they never affect the physical folder layout. The `archive/` directory is always flat, one folder per snapshot, named by timestamp. This isn't configurable (checked the storage config docs specifically; only filesystem-related options are things like `RESTRICT_FILE_NAMES` and `OUTPUT_PERMISSIONS`).

**Workaround:** build a separate symlink tree next to (not inside) the real data folder, generated from tag queries, so the real `archive/` directory stays untouched:

```bash
#!/bin/bash
DATA_DIR=/path/to/archivebox/data
OUT_DIR=/path/to/by-tag

for tag in $(archivebox list --json | jq -r '.[].tags[]' | sort -u); do
  mkdir -p "$OUT_DIR/$tag"
  archivebox list --filter-type=tag --filter-value="$tag" --json \
    | jq -r '.[].timestamp' \
    | while read -r ts; do
        ln -sfn "$DATA_DIR/archive/$ts" "$OUT_DIR/$tag/$ts"
      done
done
```

Verify actual JSON field names first (`archivebox list --json --filter-type=tag --filter-value=<tag> | head -50`) since they've shifted across versions. Run as a cron job / Unraid User Script after each import. Since a snapshot can carry multiple tags, it'll appear under more than one tag folder — correct for tags, but different from a strict one-location folder model.

---

## Alternatives considered

Evaluated against the same four requirements: Docker/Unraid, cookie or userscript import to bypass the Blogger warning, RSS-based auto-backup, and folder/tag organization.

| Tool | Docker / Unraid | Bypass Blogger warning | RSS auto-backup | Organization | Notes |
|---|---|---|---|---|---|
| **ArchiveBox** | Official Docker images; community Unraid template (may lag official releases) | Cookies via `COOKIES_FILE` or the newer "personas" system (bundles cookies + Chrome profile + user agent) | Native input parsing of feed URLs, plus `archivebox schedule` | Tags only (see issue 7 above for the filesystem gap) | Best overall fit on paper; see issues above for real-world rough edges |
| **Karakeep** (formerly Hoarder) | Docker-based install; no dedicated Unraid template found | Not supported — open GitHub issue (#414) about bypassing cookie/GDPR-style banners, unresolved | Built-in "auto-hoarding from RSS feeds" | Lists + AI-assisted tags (closer to folders than ArchiveBox) | Best organization/RSS UX of the alternatives, but likely can't get past the Blogger interstitial today |
| **Linkwarden** | Docker-based (Dockerfile + docker-compose in repo) | No cookie-import or custom-script capability found in docs | Built-in RSS feed subscription | True folder-like model: collections + sub-collections + tags | Same bypass gap as Karakeep |
| **Browsertrix** (Webrecorder) | Full platform wants Kubernetes/microk8s — heavier than plain Docker/Unraid; bare `browsertrix-crawler` container is Docker-friendly but is just a crawler | Strongest option here — log in once via a noVNC session, saved as a reusable browser profile with real session cookies; every crawl using that profile starts pre-authenticated | Not built in — the crawler alone has no scheduling/watching; would need to bolt on your own cron + feed-parsing logic | Full platform has collections/tags; bare crawler has none | Best at the actual bypass problem, worst fit for "simple Docker/Unraid" and "built-in RSS watch" |

### Bottom line from the original research

ArchiveBox was the only candidate that plausibly covers all four requirements out of the box, which is why it became the working choice — with the caveat that several pieces (full-site crawling, sitemap/feed parsing, filesystem organization) needed manual workarounds rather than being turnkey. If the Blogger bypass turns out not to work reliably even with cookies/personas, Browsertrix's login-based browser profiles are the most robust fallback for that specific problem, at the cost of a heavier deployment.
