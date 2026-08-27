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
            text/                     # <capture>.jsonl — extracted text for search
            screenshots/              # home.jpg + home.json — the site card's thumbnail
            media/                 # <capture>/ — embedded video, if the site opts in
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
- **`trash/` gives you an undo.** Deleting a site moves the directory to `trash/<id>-<slug>` and marks the row; only a purge unlinks bytes. Entries are keyed by id as well as slug because trash is flat and two sites in different folders can share a slug.

  **The retention window is a floor, not a schedule.** The sweep runs at boot and on demand, because there is no scheduler until M6 — a container left running for two months purges nothing. `0` means "on the next sweep", not "immediately at the moment of deletion", so a mistaken delete stays recoverable.

  A trashed site keeps its slug reserved: `UNIQUE(folder_id, slug)` spans deleted rows and is left that way deliberately. The visible cost is that deleting `example.com` and re-adding it before purging gives the newcomer `example-com-2`. The alternative trades that for a restored archive coming back under a suffixed name while a site created minutes ago holds its original — and between the two, the thing that has been on disk for years is the one that should keep the name it has always had.

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

CREATE TABLE login_attempts (
  id          INTEGER PRIMARY KEY,
  ts          TEXT NOT NULL,
  ip          TEXT NOT NULL DEFAULT '',
  username    TEXT NOT NULL DEFAULT '',
  successful  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX ix_login_attempts_ip_ts   ON login_attempts(ip, ts);
CREATE INDEX ix_login_attempts_user_ts ON login_attempts(username, ts);
```

**`login_attempts` is separate from `audit_log` because they have opposite
lifetimes.** The rate limiter needs a high-churn ledger it can prune
aggressively; the audit log is a record that is kept. Sharing one table would
mean either pruning the audit trail or never pruning the ledger. Both are keyed
by IP *and* by username, so neither a single address trying many accounts nor
many addresses trying one gets a free pass.

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

`path` is materialized because the folder tree is read constantly and rewritten rarely. Renaming a folder rewrites descendants' paths in one pass and moves exactly one directory — a rename carries everything under it, so a folder holding forty sites renames as fast as an empty one. `ON DELETE RESTRICT` on `parent_id` is deliberate — deleting a folder with children should be an explicit, confirmed operation, never a cascade that silently takes archives with it.

**The three name columns do different jobs**, and conflating any two of them breaks something:

| Column | Example | What it is for |
|---|---|---|
| `name` | `Photography` | What a person typed. Shown in the UI. |
| `slug` | `photography` | Uniqueness within a parent, case-folded and punctuation-stripped. |
| `path` | `Blogs/Photography` | The directory path, built from sanitized **names**. |

The directory uses the display name because the archive tree is browsed over SMB and `Blogs/Photography` is the entire point of having folders — `blogs/photography` would be a lesser version of the same thing and `f3a9/` no version of it. What sanitizing removes is what a share cannot carry: `<>:"/\|?*`, control characters, and trailing dots and spaces, which Windows silently drops so that `Photos.` and `Photos` would otherwise be one directory reached by two names.

The slug is stricter than the filesystem needs — `Foo Bar` and `Foo-Bar` both slug to `foo-bar`, and the second is refused even though they are distinct directories. That is over-strict in the safe direction: the failure it prevents is two folders in the UI silently sharing one directory on a case-insensitive filesystem.

### Moving a directory

Two operations wear the same name and they are not comparable:

- **A rename** is instant regardless of what the directory holds, because only the entry moves. This is every move inside one filesystem, which is every normal install.
- **A copy** is the only option when the ends are on different filesystems — on Unraid, `/data` spanning array disks behind FUSE. It takes minutes and holds a second copy of the bytes while it runs.

`storage.rename_directory` raises `CrossDeviceMoveError` rather than falling back silently, so that difference cannot hide inside a request. The caller turns the copy into a `move` job, which gets a progress row, cancellation and crash recovery like a capture.

The copy is **staged**: it writes to `.moving-<name>` beside the target, renames that into place, and only then deletes the source. `shutil.move` does copytree-then-rmtree straight onto the target, so a container stopped mid-copy — which on Unraid it will be — leaves a half-written directory that looks like a real archive next to a source that may already be partly gone. Staged, the same interruption leaves an intact source and a `.moving-` directory the boot sweep removes.

A move is refused while any job is queued or running for the site. wget resolves its output directory once, when the job starts; moving it underneath does not fail, it just writes the WARC into an inode the database has no name for.

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

CREATE TABLE site_health (
  site_id       INTEGER PRIMARY KEY REFERENCES sites(id) ON DELETE CASCADE,
  state         TEXT NOT NULL DEFAULT 'live', -- live|gone|moved|unreachable|blocked|error
  http_status   INTEGER,
  final_url     TEXT,                         -- where the seed ended up, if elsewhere
  error         TEXT,
  checked_at    TEXT NOT NULL,
  since         TEXT NOT NULL,                -- when the current state began
  consecutive   INTEGER NOT NULL DEFAULT 0,
  pending_state TEXT                          -- what recent checks say, not yet believed
);

CREATE TABLE annotations (
  id          INTEGER PRIMARY KEY,
  site_id     INTEGER NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
  url         TEXT NOT NULL,
  quote       TEXT NOT NULL,                  -- the anchor
  prefix      TEXT NOT NULL DEFAULT '',       -- context before, to disambiguate
  suffix      TEXT NOT NULL DEFAULT '',       -- context after
  block_index INTEGER NOT NULL DEFAULT 0,     -- a hint, not the anchor
  note        TEXT,
  color       TEXT NOT NULL DEFAULT 'yellow',
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);
CREATE INDEX ix_annotations_page ON annotations(site_id, url);
```

**`site_health` is one row per site, overwritten in place**, because it is a
current state rather than a history. The only historical fact worth keeping is
`since` — the moment the current state began — which is what turns "returning
404" into "has been returning 404 since March". It is a separate table because
every other column on `sites` describes the archive and these describe somebody
else's server. `pending_state` and `consecutive` are what stop one bad minute
announcing that a site is gone: a state changes only once enough checks agree.

**An annotation is anchored by its quotation, not by an offset.** The text it
points into is derived: re-extracting a capture rewrites the JSONL and a later
capture of the same page has different offsets again, so a byte range would
orphan every note in the archive on the next extraction. Quote plus a little
context survives both, and a quote that genuinely cannot be found is *reported*
rather than moved to a sentence it did not mark. `block_index` is a hint that
makes the common case one string search instead of a scan; being wrong costs
only speed.

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
  auto_capture   INTEGER NOT NULL DEFAULT 1,
  recapture_on_update INTEGER NOT NULL DEFAULT 0,
  next_poll_at   TEXT,                           -- the schedule itself; see below
  last_polled_at TEXT,
  last_success_at TEXT,
  last_status    INTEGER,
  etag           TEXT,
  last_modified  TEXT,
  consecutive_failures INTEGER NOT NULL DEFAULT 0,
  last_error     TEXT,
  disabled_reason TEXT,                          -- set when the tool switched it off
  UNIQUE(site_id, url)
);
CREATE INDEX ix_feeds_due ON feeds(enabled, next_poll_at);

CREATE TABLE feed_items (
  id          INTEGER PRIMARY KEY,
  feed_id     INTEGER NOT NULL REFERENCES feeds(id) ON DELETE CASCADE,
  guid        TEXT NOT NULL,
  url         TEXT NOT NULL,                     -- raw: what gets fetched
  canonical_url TEXT NOT NULL DEFAULT '',        -- normalized: what gets compared
  title       TEXT,
  published_at TEXT,
  updated_at  TEXT,
  first_seen_at TEXT NOT NULL,
  last_seen_at  TEXT NOT NULL,
  gone_at     TEXT,                              -- sitemaps only; see below
  capture_id  INTEGER REFERENCES captures(id) ON DELETE SET NULL,
  status      TEXT NOT NULL DEFAULT 'pending', -- pending|captured|skipped|failed
  UNIQUE(feed_id, guid)
);

-- Every poll, whether or not anything came of it. This table is the milestone
-- as much as the polling is: the ArchiveBox note that a `curl | grep` cron was
-- more dependable than the tool's scheduler was a judgement about
-- observability, and a scheduler you cannot inspect is one you will not trust.
CREATE TABLE feed_polls (
  id          INTEGER PRIMARY KEY,
  feed_id     INTEGER NOT NULL REFERENCES feeds(id) ON DELETE CASCADE,
  ts          TEXT NOT NULL,
  status      INTEGER NOT NULL DEFAULT 0,        -- 0 when there was no response at all
  duration_ms INTEGER NOT NULL DEFAULT 0,
  entries_seen INTEGER NOT NULL DEFAULT 0,
  new_items   INTEGER NOT NULL DEFAULT 0,
  gone_items  INTEGER NOT NULL DEFAULT 0,
  action      TEXT NOT NULL DEFAULT '',
  job_id      INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
  error       TEXT
);
```

**`next_poll_at` is the schedule, not a derivation of it.** Jitter has to be stored somewhere or it is not jitter, and a stored due time makes "what should run now" one indexed comparison whose answer does not depend on how long the container was stopped.

**`canonical_url` is a second dedup key, not a convenience.** Some platforms regenerate a post's guid whenever it is edited; keyed on the guid alone, one editorial pass re-captures the entire archive.

**`gone_at` is only ever set from a sitemap.** A feed carries the most recent N entries, so an entry leaving one is the normal course of events and means nothing. A sitemap is meant to be complete, so a URL vanishing from one is the "a post you archived no longer exists upstream" event. Inferring it from feeds would fire that alert on every poll of every healthy blog.

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
| `/data/by-tag/**` | Symlink tree rebuild | After any tag, folder or site change; at boot |
| `/data/replay/config.yaml` and `collections/**` | `cairn replay-init` | At boot; on demand |
| `derived/text/**` | Text extraction post-processor | After capture (M8) |
| `sites.size_bytes`, `url_count` | Stats rollup | After capture; nightly |
| Checksum verification | Integrity job | Weekly; reports mismatches, never auto-repairs |
| Extracted text | `derived/text/<capture>.jsonl` | Written after each capture; regenerable from the WARCs |
| Search index | `page_text` + `page_text_fts` | Contentless FTS5 — terms only. Rebuilt from `derived/text/` |

**The search index holds no copy of the archive.** `page_text` records where each page's text is — the JSONL file, the byte offset, the length — and the FTS5 table beside it is `content=''`, so it stores the terms and nothing else. Measured over the same corpus, an ordinary FTS5 table costs 1.29x the raw text and a contentless one 0.21x; this database is copied whole before every migration with ten backups kept, so the ordinary form would have multiplied a gigabyte of extracted text into nearly thirteen on the cache pool. The cost is that `snippet()` returns NULL on a contentless table, so result snippets are built by seeking into the JSONL — which turns out to read better anyway, because that text is the de-boilerplated version.

The integrity job matters more than it sounds. Bit rot on a NAS array is real, WARCs are cold data nobody reads for years, and a weekly pass comparing `sha256` against `manifest.json` is the difference between noticing in a week and noticing never.

### The tag tree is rebuilt whole, and its links are relative

This document originally called for a debounced incremental refresh of `/data/by-tag`. It is rebuilt wholesale instead. At any scale this tool reaches, a rebuild is a few hundred `symlink(2)` calls and finishes faster than the request that triggered it — so incremental was identical in cost and carried the one failure mode that matters: a tree that quietly stops matching the database and looks fine until somebody trusts it.

**The links must be relative.** The tree is read over SMB, where `/data` is not the root of anything: the share is mounted as `Z:\` or `/mnt/tower/cairn`, and an absolute `/data/archives/…` link resolves against the *client's* filesystem, where it means nothing. Samba compounds it — `wide links` defaults to off, so a link whose target appears to leave the share is refused outright. `../../archives/Blogs/example` stays inside the share and is followed on both counts.

**A symlink carries a type, and the type is fixed when it is created.** File or directory, inferred from whether the target exists — so a link written before its target does becomes a *file* link. Linux resolves by path at every access and never notices; a Windows-backed filesystem bakes the answer in, and the entry shows as a 0 KB file for good, even once the directory appears. Two rules follow, and both exist because this shipped once:

- Nothing links ahead of its target. `create_site` makes the site directory before it touches the tag tree, and `_link` refuses a target that is not there.
- A rebuild recreates every link rather than leaving alone the ones whose text already matches. That optimisation is what made a mistyped link unrepairable — the text was right, only the type was wrong, and the type is invisible from the Linux side. `cairn rebuild-symlinks` is therefore a real repair.

Naming inside a tag directory is computed from the database, never from what is already on disk. Site slugs are unique within a folder, not globally, so two sites can both be `example`; when that happens inside one tag, **both** get their id appended rather than the newcomer alone. A name that depends on which row arrived first cannot be recomputed, and a tree that cannot be recomputed cannot be checked.

Pruning only ever removes symlinks and the empty directories that held them. A real directory under `by-tag` was put there by hand over the share, and deleting it because it is not in the database would be this tool destroying something it never owned.

---

## Why there is no storage tiering

docs/13 asked for captures older than N months to move to a slower tier, with
the index staying local so replay resolves and the bytes fetched on demand.
Probed against the pinned pywb before writing any of it, and the probe settled
it three ways:

**Replay does not care where the bytes are.** A WARC moved out of the
collection and replaced by a symlink replayed identically to the control —
same 200, same archived body. So the replay half is free.

**Every other reader refuses it.** `storage.resolve_within` resolves symlinks
*before* checking containment, deliberately: a symlink planted inside a capture
directory must not be usable to record a file outside it (docs/05). Measured, a
symlink out of the site directory raises `StoragePathError` and one inside it
resolves. So integrity verification, WACZ export and text extraction would all
refuse a file replay serves perfectly — and making them accept it means
removing the control that keeps an engine inside the archive tree.

**A tier that can go away is an unexplained 503.** With the symlink's target
renamed, replay answered 503 — indistinguishable from the pruned-dedup-source
503 M8 documented. Fetching on demand would require sitting in front of pywb,
and nothing here ever proxies replayed bytes (docs/07).

Satisfying the second point means the tier has to live inside each site's own
directory, which is a move within one directory tree and therefore usually no
move at all.

**And the platform already does it better.** Unraid's shfs presents one path
whatever device a file is on; a share's cache setting moves files between pool
and array transparently, with no index to keep in step. An archive that should
leave the machine altogether has the WACZ export, which is self-describing and
needs nothing here to read it.
