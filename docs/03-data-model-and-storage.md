# 03 — Data Model & Storage

## Guiding principle

**The database is an index over the filesystem, not the system of record.** Every site directory carries a `site.yaml`; every capture directory carries a `manifest.json`. Losing `cairn.db` costs you tags, schedules, and job history — a rebuild job walks `/data/archives` and reconstructs sites, captures, and URL tables from the manifests and the WARCs themselves. Design every schema change with "can this be reconstructed from disk?" in mind.

---

## On-disk layout

```
/config/                              # cache pool — small, hot, backed up nightly
  cairn.db                            # SQLite (+ -wal, -shm)
  secret.key                          # only if not supplied via env
  engines/                            # addon engine drop-ins
    <engine-id>/engine.yaml
  pywb/
    config.yaml                       # generated; do not hand-edit
  logs/

/data/                                # array — large, cold
  archives/
    Blogs/                            # ← folder tree mirrors the UI exactly
      Photography/
        example-blog/                 # ← site directory = one self-contained unit
          site.yaml
          captures/
            20260809T142530Z-full-wget/
              manifest.json
              crawl.log
              wget.cdx                # for the next run's --warc-dedup only
              warc/
                part-00000.warc.gz
                part-00001.warc.gz
              files/                  # only if "keep mirror" is on
            20260811T090000Z-feed-wget/
              …
          index/
            site.cdxj                 # merged across all captures — what pywb reads
          derived/
            text/                     # extracted text for search (M8)
            screenshots/
            media/
          exports/
            example-blog-2026-08.wacz
  by-tag/                             # generated symlink tree
    travel/
      example-blog -> ../../archives/Blogs/Photography/example-blog
  personas/
    <profile-id>/
      cookies.txt.enc
      script.user.js.enc
      meta.json                       # non-secret: hosts covered, expiry, fingerprint
  tmp/                                # wget --warc-tempdir, staging; wiped on boot
  trash/                              # soft-deleted sites await purge
```

### Rules

- **A site directory is atomic and movable.** Everything about a site lives under it. Moving it between folders is a rename; nothing else needs updating except the DB path and the symlink tree.
- **Capture directories are immutable once complete.** Name format `<UTC ISO8601 basic>-<kind>-<engine>`, e.g. `20260809T142530Z-full-wget`. Sorts chronologically, self-describing, no lookup needed.
- **Slugs are sanitized and stable.** Lowercase, `[a-z0-9-]` only, collisions get `-2`. The slug never changes when the display title changes — renaming a site must not move its files.
- **`tmp/` is on the same filesystem as `archives/`** so finished WARCs move by rename, not copy. This matters: `--warc-tempdir` on a different mount turns every segment close into a full byte copy.
- **`trash/` gives you an undo.** Deleting a site moves the directory there and schedules a purge after N days (default 30, configurable, `0` = immediate).

### Storage sizing

Rough planning numbers for a text-and-images blog: **1.5–4 MB per post** including page requisites, so a 2,000-post blog lands around 3–8 GB per full capture. Incremental feed captures with `--warc-dedup` against the prior CDX are typically **2–5% of a full capture**. Budget 1.5× the raw total for index, derived text, and one WACZ export.

The two things that blow this up are (a) leaving "keep mirrored files" on, which doubles it, and (b) re-running full captures on a schedule instead of incrementals. Both are UI-visible with warnings.

---

## Database schema

SQLite. Every connection sets `PRAGMA journal_mode=WAL; foreign_keys=ON; busy_timeout=5000; synchronous=NORMAL;`.

### Identity & auth

```sql
CREATE TABLE users (
  id             INTEGER PRIMARY KEY,
  username       TEXT NOT NULL UNIQUE,
  password_hash  TEXT NOT NULL,              -- argon2id
  totp_secret    BLOB,                       -- sealed; NULL = 2FA off
  totp_confirmed INTEGER NOT NULL DEFAULT 0,
  created_at     TEXT NOT NULL,
  last_login_at  TEXT,
  failed_logins  INTEGER NOT NULL DEFAULT 0,
  locked_until   TEXT
);

CREATE TABLE sessions (
  id          TEXT PRIMARY KEY,              -- 256-bit random, stored hashed
  user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at  TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  expires_at  TEXT NOT NULL,
  user_agent  TEXT,
  ip          TEXT,
  revoked_at  TEXT
);

CREATE TABLE audit_log (
  id         INTEGER PRIMARY KEY,
  ts         TEXT NOT NULL,
  actor      TEXT,
  action     TEXT NOT NULL,                  -- login.ok, login.fail, site.delete, profile.write …
  target     TEXT,
  detail     TEXT,                           -- JSON; never contains secret values
  ip         TEXT
);
CREATE INDEX idx_audit_ts ON audit_log(ts DESC);
```

### Folders and tags

```sql
CREATE TABLE folders (
  id         INTEGER PRIMARY KEY,
  parent_id  INTEGER REFERENCES folders(id) ON DELETE RESTRICT,
  name       TEXT NOT NULL,
  slug       TEXT NOT NULL,
  path       TEXT NOT NULL UNIQUE,           -- materialized: 'Blogs/Photography'
  sort_order INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  UNIQUE(parent_id, slug)
);

CREATE TABLE tags (
  id     INTEGER PRIMARY KEY,
  name   TEXT NOT NULL UNIQUE,
  slug   TEXT NOT NULL UNIQUE,
  color  TEXT,
  description TEXT
);

CREATE TABLE site_tags (
  site_id INTEGER NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
  tag_id  INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
  PRIMARY KEY (site_id, tag_id)
);
```

`path` is materialized because the folder tree is read constantly and rewritten rarely. Renaming a folder rewrites descendants' paths in one `UPDATE ... WHERE path LIKE 'old/%'` and schedules a directory rename job. `ON DELETE RESTRICT` on `parent_id` is deliberate — deleting a folder with children should be an explicit, confirmed operation, never a cascade that silently takes archives with it.

### Sites

```sql
CREATE TABLE sites (
  id            INTEGER PRIMARY KEY,
  folder_id     INTEGER NOT NULL REFERENCES folders(id) ON DELETE RESTRICT,
  slug          TEXT NOT NULL,
  title         TEXT NOT NULL,
  seed_url      TEXT NOT NULL,
  primary_host  TEXT NOT NULL,
  notes         TEXT,
  profile_id    INTEGER REFERENCES access_profiles(id) ON DELETE SET NULL,
  engine_id     TEXT NOT NULL DEFAULT 'wget-warc',
  engine_config TEXT NOT NULL DEFAULT '{}',  -- JSON, validated against engine schema
  scope_settings TEXT NOT NULL DEFAULT '{}', -- JSON: limits, robots, politeness
  keep_mirror   INTEGER NOT NULL DEFAULT 0,
  status        TEXT NOT NULL DEFAULT 'new', -- new|indexed|ready|capturing|error|archived
  archive_path  TEXT NOT NULL UNIQUE,        -- relative to /data/archives
  size_bytes    INTEGER NOT NULL DEFAULT 0,
  url_count     INTEGER NOT NULL DEFAULT 0,
  last_capture_at TEXT,
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL,
  deleted_at    TEXT,
  UNIQUE(folder_id, slug)
);
CREATE INDEX idx_sites_folder ON sites(folder_id) WHERE deleted_at IS NULL;
CREATE INDEX idx_sites_host   ON sites(primary_host);
```

### Discovery & scope

```sql
CREATE TABLE discoveries (
  id           INTEGER PRIMARY KEY,
  site_id      INTEGER NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
  job_id       INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
  started_at   TEXT NOT NULL,
  finished_at  TEXT,
  pages_fetched INTEGER NOT NULL DEFAULT 0,
  urls_found    INTEGER NOT NULL DEFAULT 0,
  summary       TEXT                          -- JSON: sitemaps, feeds, robots, pagination
);

CREATE TABLE discovered_hosts (
  id            INTEGER PRIMARY KEY,
  discovery_id  INTEGER NOT NULL REFERENCES discoveries(id) ON DELETE CASCADE,
  host          TEXT NOT NULL,
  registrable   TEXT NOT NULL,               -- via PSL, e.g. blogspot.com
  is_seed_host  INTEGER NOT NULL DEFAULT 0,
  link_refs     INTEGER NOT NULL DEFAULT 0,  -- times seen as a page link
  asset_refs    INTEGER NOT NULL DEFAULT 0,  -- times seen as a subresource
  distinct_urls INTEGER NOT NULL DEFAULT 0,
  role_guess    TEXT,                        -- self|cdn|images|fonts|analytics|social|comments|unknown
  sample_urls   TEXT,                        -- JSON array, max 5
  UNIQUE(discovery_id, host)
);

CREATE TABLE scope_rules (
  id            INTEGER PRIMARY KEY,
  site_id       INTEGER NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
  host          TEXT NOT NULL,
  crawl_pages   INTEGER NOT NULL DEFAULT 0,  -- follow links on this host
  fetch_assets  INTEGER NOT NULL DEFAULT 1,  -- allow subresources from this host
  path_prefix   TEXT,                        -- optional narrowing
  allow_extensionless INTEGER NOT NULL DEFAULT 0,  -- see 04; no regex can infer this
  UNIQUE(site_id, host)
);

CREATE TABLE scope_patterns (
  id       INTEGER PRIMARY KEY,
  site_id  INTEGER NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
  kind     TEXT NOT NULL,                    -- accept|reject
  pattern  TEXT NOT NULL,                    -- regex
  note     TEXT
);
```

Splitting `crawl_pages` from `fetch_assets` is the schema expression of R6: you want `1.bp.blogspot.com` images without treating it as a site to crawl. Collapsing these into one boolean is the most likely early mistake.

`allow_extensionless` is a column rather than a derived value because it cannot be derived: an extension-less image URL and an extension-less page URL are textually identical ([04](04-discovery-and-scoping.md#what-running-it-actually-established)).

**`scope_settings` is separate from `engine_config` and must stay that way.** `engine_config` is validated against the engine's own JSON Schema, and every built-in declares `additionalProperties: false` — so a site-level crawl setting stored there fails validation the moment a capture starts. They look like the same kind of bag and are not.

### Captures & URLs

```sql
CREATE TABLE captures (
  id            INTEGER PRIMARY KEY,
  site_id       INTEGER NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
  job_id        INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
  kind          TEXT NOT NULL,               -- full|incremental|feed|manual|resume
  engine_id     TEXT NOT NULL,
  engine_version TEXT,
  dir_name      TEXT NOT NULL,               -- 20260809T142530Z-full-wget
  started_at    TEXT NOT NULL,
  finished_at   TEXT,
  status        TEXT NOT NULL,               -- running|ok|partial|failed|cancelled|interrupted
  url_count     INTEGER NOT NULL DEFAULT 0,
  error_count   INTEGER NOT NULL DEFAULT 0,
  bytes_written INTEGER NOT NULL DEFAULT 0,
  warc_files    TEXT,                        -- JSON: [{name, size, sha256}]
  indexed_at    TEXT,
  UNIQUE(site_id, dir_name)
);

CREATE TABLE capture_urls (
  id          INTEGER PRIMARY KEY,
  capture_id  INTEGER NOT NULL REFERENCES captures(id) ON DELETE CASCADE,
  url         TEXT NOT NULL,
  host        TEXT NOT NULL,
  status_code INTEGER,
  mime        TEXT,
  size_bytes  INTEGER,
  digest      TEXT,                          -- payload sha1, base32 — matches CDXJ
  is_revisit  INTEGER NOT NULL DEFAULT 0,    -- deduped against a prior capture
  fetched_at  TEXT,
  error       TEXT
);
CREATE INDEX idx_curls_capture ON capture_urls(capture_id);
CREATE INDEX idx_curls_url     ON capture_urls(url);
CREATE INDEX idx_curls_errors  ON capture_urls(capture_id) WHERE status_code >= 400 OR error IS NOT NULL;
```

`capture_urls` is the highest-volume table by far — hundreds of thousands of rows across a mature instance. Insert in batched transactions (500–1000 rows), never per-event. Add a retention policy that prunes `capture_urls` for superseded captures after N days; the CDXJ index remains the authoritative URL list.

### Feeds

```sql
CREATE TABLE feeds (
  id             INTEGER PRIMARY KEY,
  site_id        INTEGER NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
  url            TEXT NOT NULL,
  kind           TEXT NOT NULL DEFAULT 'auto',   -- auto|rss|atom|sitemap|json
  title          TEXT,
  enabled        INTEGER NOT NULL DEFAULT 1,
  interval_min   INTEGER NOT NULL DEFAULT 360,
  last_polled_at TEXT,
  last_success_at TEXT,
  etag           TEXT,
  last_modified  TEXT,
  consecutive_failures INTEGER NOT NULL DEFAULT 0,
  last_error     TEXT,
  UNIQUE(site_id, url)
);

CREATE TABLE feed_items (
  id          INTEGER PRIMARY KEY,
  feed_id     INTEGER NOT NULL REFERENCES feeds(id) ON DELETE CASCADE,
  guid        TEXT NOT NULL,
  url         TEXT NOT NULL,
  title       TEXT,
  published_at TEXT,
  first_seen_at TEXT NOT NULL,
  capture_id  INTEGER REFERENCES captures(id) ON DELETE SET NULL,
  status      TEXT NOT NULL DEFAULT 'pending', -- pending|captured|skipped|failed
  UNIQUE(feed_id, guid)
);
```

### Access profiles

```sql
CREATE TABLE access_profiles (
  id           INTEGER PRIMARY KEY,
  name         TEXT NOT NULL UNIQUE,
  mode         TEXT NOT NULL,                -- none|cookies|userscript|interactive
  user_agent   TEXT,
  hosts        TEXT,                         -- JSON array the profile is valid for
  cookies_enc  BLOB,                         -- sealed Netscape cookies.txt
  script_enc   BLOB,                         -- sealed userscript
  minted_at    TEXT,                         -- last successful mint (userscript/interactive)
  expires_at   TEXT,                         -- earliest cookie expiry, for warnings
  fingerprint  TEXT,                         -- sha256 of plaintext, for change detection
  last_verified_at TEXT,
  verify_url   TEXT,                         -- URL used to test the profile
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL
);
```

No column here is ever serialized to an API response. `GET` returns `{name, mode, hosts, minted_at, expires_at, fingerprint, last_verified_at}` only.

### Jobs & engines

```sql
CREATE TABLE jobs (
  id           INTEGER PRIMARY KEY,
  type         TEXT NOT NULL,                -- discovery|capture|mint|index|export|move|verify|purge
  site_id      INTEGER REFERENCES sites(id) ON DELETE CASCADE,
  status       TEXT NOT NULL,                -- queued|running|ok|failed|cancelled|interrupted
  priority     INTEGER NOT NULL DEFAULT 100,
  spec         TEXT NOT NULL,                -- JSON job spec handed to the engine
  progress     TEXT,                         -- JSON, updated ~1 Hz
  pid          INTEGER,
  queued_at    TEXT NOT NULL,
  started_at   TEXT,
  finished_at  TEXT,
  error        TEXT,
  attempts     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_jobs_queue ON jobs(status, priority, queued_at);

CREATE TABLE engines (
  id           TEXT PRIMARY KEY,             -- wget-warc, browsertrix, …
  name         TEXT NOT NULL,
  version      TEXT NOT NULL,
  source       TEXT NOT NULL,                -- builtin|dropin|docker
  manifest     TEXT NOT NULL,                -- full engine.yaml as JSON
  enabled      INTEGER NOT NULL DEFAULT 1,
  installed_at TEXT NOT NULL,
  last_error   TEXT
);

CREATE TABLE settings (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL                        -- JSON
);

CREATE TABLE saved_views (
  id      INTEGER PRIMARY KEY,
  name    TEXT NOT NULL UNIQUE,
  query   TEXT NOT NULL,                     -- JSON filter expression
  pinned  INTEGER NOT NULL DEFAULT 0
);
```

---

## `site.yaml` — the disk-side record

Written on every site change. This plus `manifest.json` is what makes the DB rebuildable.

```yaml
schema: 1
id: 42
slug: example-blog
title: "Example Blog"
seed_url: https://example.blogspot.com/
primary_host: example.blogspot.com
folder: Blogs/Photography
tags: [travel, photography]
engine: wget-warc
access_profile: blogger-interstitial     # name only — never the material
scope:
  hosts:
    - {host: example.blogspot.com,      crawl_pages: true,  fetch_assets: true}
    - {host: 1.bp.blogspot.com,         crawl_pages: false, fetch_assets: true}
    - {host: blogger.googleusercontent.com, crawl_pages: false, fetch_assets: true}
  reject_patterns:
    - '[?&]m=1'
    - '[?&]replytocom='
feeds:
  - url: https://example.blogspot.com/feeds/posts/default
    interval_min: 360
created_at: 2026-08-09T14:20:00Z
```

## `manifest.json` — the capture record

```json
{
  "schema": 1,
  "capture_id": 128,
  "site_slug": "example-blog",
  "kind": "full",
  "engine": {"id": "wget-warc", "version": "1.0.0", "tool_version": "GNU Wget 1.21.4"},
  "started_at": "2026-08-09T14:25:30Z",
  "finished_at": "2026-08-09T16:02:11Z",
  "status": "ok",
  "seeds": ["https://example.blogspot.com/"],
  "seed_source": {"sitemap": 1834, "feed": 25, "manual": 1},
  "scope": { "...": "resolved scope as passed to the engine" },
  "stats": {"urls": 1847, "errors": 12, "revisits": 0, "bytes": 4182937600},
  "warc_files": [
    {"name": "warc/part-00000.warc.gz", "size": 1073741824, "sha256": "…"},
    {"name": "warc/part-00001.warc.gz", "size": 812304128,  "sha256": "…"}
  ],
  "index": {"file": "../../index/site.cdxj", "records": 1847, "built_at": "2026-08-09T16:05:02Z"}
}
```

---

## Derived data and maintenance jobs

Everything below is regenerable and safe to delete:

| Artifact | Rebuilt by | When |
|---|---|---|
| `index/site.cdxj` | `cdxj-indexer` over the site's WARCs | After every capture; on demand |
| `/data/by-tag/**` | Symlink tree refresh | After tag/folder/site changes, debounced |
| `/config/pywb/config.yaml` | pywb config generator | After any collection change |
| `derived/text/**` | Text extraction post-processor | After capture (M8) |
| `sites.size_bytes`, `url_count` | Stats rollup | After capture; nightly |
| Checksum verification | Integrity job | Weekly; reports mismatches, never auto-repairs |

The integrity job matters more than it sounds. Bit rot on a NAS array is real, WARCs are cold data nobody reads for years, and a weekly pass comparing `sha256` against `manifest.json` is the difference between noticing in a week and noticing never.
