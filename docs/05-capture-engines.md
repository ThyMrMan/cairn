# 05 — Capture Engines & the Addon System

Covers R8. An engine is anything that takes a scope + seeds and produces archival artifacts. Core knows the contract; it knows nothing about wget.

---

## Why the seam exists from day one

wget is a good default and a hard ceiling: no JavaScript, no lazy-loaded images, no infinite scroll, no post-login SPA. Any real Blogger archive eventually hits at least the lazy-image problem. Building the interface after the fact means threading it back through the job runner, the schema, and every UI surface that shows capture options — so it goes in with the first engine ([D3](00-decisions.md#d3--wget-for-v1-behind-an-engine-interface-from-day-one)).

---

## Engine manifest

An engine is a directory containing `engine.yaml`. Built-ins ship in the image; addons drop into `/config/engines/<id>/`, discovered on startup and on demand from the UI.

```yaml
apiVersion: cairn.engine/v1
id: wget-warc
name: "wget (WARC)"
version: "1.0.0"
description: "Recursive crawl to WARC using GNU wget. Fast, no JavaScript."
author: "built-in"
homepage: https://www.gnu.org/software/wget/

runtime:
  type: subprocess                  # subprocess | docker
  command: ["python", "-m", "cairn.engines.wget"]
  # docker runtime instead:
  # image: my/engine:1.0.0
  # args: ["/cairn/job/job.json"]   # optional; this is the default
  # shm_size: 2g

capabilities:
  outputs: [warc, cdx, files, log]  # what it can produce
  javascript: false                 # can it execute page JS?
  scope: [host, path, regex]        # scope dimensions it enforces
  auth: [cookies, headers, user_agent]
  incremental: true                 # supports dedup against a prior capture
  resumable: false
  max_concurrency: 1
  requires_browser: false

preflight:                          # optional; runs before the engine
  - mint_cookies                    # only if the site's profile is userscript/interactive

config_schema:                      # JSON Schema → auto-generated UI form
  type: object
  additionalProperties: false
  properties:
    wait_s:
      type: number
      title: "Delay between requests (seconds)"
      minimum: 0
      default: 1.0
    random_wait:
      type: boolean
      title: "Randomize delay (0.5×–1.5×)"
      default: true
    rate_limit:
      type: string
      title: "Bandwidth limit"
      pattern: "^[0-9]+[kmKM]?$"
      default: "2m"
    tries:
      type: integer
      title: "Retries per URL"
      minimum: 0
      maximum: 20
      default: 3
    timeout_s:
      type: integer
      title: "Per-request timeout"
      default: 30
    warc_max_size:
      type: string
      title: "WARC segment size"
      enum: ["500M", "1G", "2G", "5G"]
      default: "1G"
    keep_mirror:
      type: boolean
      title: "Also keep plain files on disk"
      description: "Roughly doubles storage. WARC alone is sufficient for replay."
      default: false
    content_on_error:
      type: boolean
      title: "Archive error page bodies (404s etc.)"
      default: true
    user_agent:
      type: string
      title: "User agent"
      default: "Mozilla/5.0 (compatible; Cairn/1.0; +https://github.com/you/cairn)"
```

> **Built in M7, and the `docker` sketch above was wrong.** It showed a stock
> third-party image run with templated arguments and hand-written mounts, and
> that cannot work for two reasons.
>
> **The mounts cannot be written by the engine author.** Cairn's `/data` is not
> the daemon's `/data`: the daemon resolves every path on the *host*, so a
> bind of our own `/data/archives/…` asks for something that does not exist
> there. Probed on a real daemon — our `/data` came from a named volume at
> `/var/lib/docker/volumes/…/_data`, our `/config` from
> `/run/desktop/mnt/host/c/Coding/Website Backup`, space and all. Cairn works
> out the mounts itself by looking up which of *its own* mounts contains each
> directory (`VolumeOptions.Subpath` for a volume, a composed host path for a
> bind), and the container always sees the same two locations:
> **`/cairn/job`** and **`/cairn/out`**, with a `job.json` rewritten to match.
> An image cannot be written against paths that depend on somebody's array
> layout.
>
> `--volumes-from` would have reproduced everything at our own paths — it
> works, it was tested — but "everything" includes `/config`, which holds the
> database and the master key that decrypts every stored cookie jar. The
> precise mounts were verified to leave `/config` invisible to the engine.
>
> **A stock image does not speak the protocol.** `runtime: docker` means "run
> my image and read its stdout as cairn NDJSON", exactly as the subprocess
> type does. browsertrix writes its own JSON log format, so it is wrapped by
> an *adapter engine* (`cairn/engines/browsertrix.py`) rather than run
> directly. Putting the translation inside the runtime would give the runtime
> an "and which log format?" field, which is where a clean seam turns into a
> pile of special cases.
>
> **A relative `command` resolves against the engine's own directory.**
> `command: ["python3", "engine.py"]` is the obvious thing to write and could
> never have worked otherwise — an engine runs with the *job's* temp directory
> as its working directory. Any argument naming a file that exists in the
> engine's directory is made absolute; `-m` and module names are left alone.
> Found by the conformance harness on its first run against the template,
> which is exactly what the harness is for.

### Config schema → generated UI

The JSON Schema is rendered directly into a form (title, description, type, enum, min/max, default). An addon author writes zero frontend code and gets validated, labeled controls. Core validates submitted config against the same schema server-side before persisting — never trust the client to have applied the constraints.

Supported keywords for form generation: `type`, `title`, `description`, `default`, `enum`, `minimum`, `maximum`, `pattern`, `format` (`uri`, `duration`), and `x-cairn-widget` (`textarea`, `password`, `host-list`, `regex-list`) for cases the base vocabulary can't express.

---

## Job protocol

### In — the job spec

Core writes `job.json` into a fresh job directory and passes its path as `argv[1]`.

```json
{
  "protocol": "cairn.engine/v1",
  "job_id": 512,
  "job_type": "capture",
  "site": {"id": 42, "slug": "example-blog", "title": "Example Blog"},
  "output_dir": "/data/archives/Blogs/Photography/example-blog/captures/20260809T142530Z-full-wget",
  "temp_dir": "/data/tmp/job-512",
  "seeds": ["https://example.blogspot.com/"],
  "seed_file": "seeds.txt",
  "scope": { "...": "see 04 — resolved scope object" },
  "auth": {
    "cookies_file": "/data/tmp/job-512/cookies.txt",
    "user_agent": "Mozilla/5.0 …",
    "headers": {"Accept-Language": "en-US,en;q=0.9"}
  },
  "incremental": {
    "dedup_cdx": "/data/archives/…/captures/20260801T…-full-wget/wget.cdx"
  },
  "config": {"wait_s": 1.0, "random_wait": true, "rate_limit": "2m", "…": "…"},
  "limits": {"max_bytes": 21474836480, "max_duration_s": 86400, "free_space_floor_bytes": 10737418240}
}
```

`seed_file` is a newline-delimited list written next to `job.json` containing **every** URL from sitemaps and feeds. This is the mechanism that sidesteps ArchiveBox's depth ceiling entirely (its issues 1 and 4): the crawler is handed the complete URL set up front and link-following becomes a supplement, not the primary discovery mechanism.

### Out — NDJSON on stdout

One JSON object per line, flushed immediately. Anything the engine writes to **stderr** is captured verbatim as diagnostic output and shown on failure.

```jsonc
{"type":"started","ts":"2026-08-09T14:25:31Z","tool_version":"GNU Wget 1.21.4"}
{"type":"log","ts":"…","level":"info","msg":"Loaded 1834 seed URLs"}
{"type":"url","ts":"…","url":"https://example.blogspot.com/2019/04/post.html",
 "status":200,"mime":"text/html","size":48213,
 "digest":"sha1:XQ3…","revisit":false}
{"type":"url","ts":"…","url":"https://example.blogspot.com/missing","status":404,"error":"Not Found"}
{"type":"progress","ts":"…","done":412,"total":1847,"bytes":183042110,"rate_bps":204800,"eta_s":4120}
{"type":"artifact","ts":"…","kind":"warc","path":"warc/part-00000.warc.gz",
 "size":1073741824,"sha256":"…"}
{"type":"warning","ts":"…","code":"interstitial_detected",
 "msg":"Response matched content-warning heuristic","url":"https://…"}
{"type":"result","ts":"…","status":"ok",
 "stats":{"urls":1847,"errors":12,"revisits":0,"bytes":4182937600}}
```

| Event | Handling |
|---|---|
| `started` | Record tool version into `manifest.json` |
| `log` | Appended to `crawl.log`; streamed to the UI |
| `url` | Batched into `capture_urls` (500–1000 per transaction) |
| `progress` | Throttled to ~1 Hz into `jobs.progress`; drives the progress bar |
| `artifact` | Recorded with checksum; verified against disk on completion |
| `warning` | Surfaced in the UI; `interstitial_detected` triggers cookie re-mint |
| `result` | Terminal state; must be the last line |

**Contract rules.**
- Exit `0` with a `result` line = success. Non-zero, or exit without `result`, = failure.
- Writes go only inside `output_dir` and `temp_dir`. Core enforces this by resolving symlinks and rejecting escapes before recording artifacts.
- Malformed stdout lines are logged and skipped, never fatal — an engine that prints a stray line shouldn't kill a six-hour crawl.
- `SIGTERM` means finish the current record, close and flush WARCs, emit `result` with `status: "partial"`, exit. Core waits `grace_period_s` (default 60) then sends `SIGKILL`.
- Engines must be safe to re-run against the same site — never "resume mid-stream," always "re-crawl and dedup."

### Failure statuses

`ok` (everything fetched), `partial` (some URLs failed or cancelled — artifacts still valid and indexed), `failed` (no usable output).

Partial is the common case on real sites and must be a first-class outcome. A crawl that got 1,835 of 1,847 pages is a successful archive with 12 known gaps, and the UI should present it that way — with the failures listed and individually retryable — not as a red X.

---

## The `wget-warc` engine

A thin Python wrapper: build argv, spawn wget, parse its log into events, register artifacts.

### Command construction

```python
argv = [
    "wget",
    # ── WARC output ────────────────────────────────────────────────
    "--warc-file",
    str(out / "warc" / "part"),  # wget appends -NNNNN.warc.gz
    "--warc-cdx",  # for the NEXT run's --warc-dedup
    "--warc-max-size",
    cfg["warc_max_size"],
    "--warc-tempdir",
    str(tmp),  # MUST be same filesystem as out
    "--warc-header",
    f"operator: cairn",
    "--warc-header",
    f"isPartOf: {site['slug']}",
    "--warc-header",
    f"description: {capture_label}",
    "--warc-header",
    f"http-header-user-agent: {ua}",
    # ── recursion & scope ──────────────────────────────────────────
    "--recursive",
    "--level=inf",
    "--page-requisites",
    "--span-hosts",
    f"--domains={','.join(allowed_hosts)}",
    # ── politeness ─────────────────────────────────────────────────
    f"--wait={cfg['wait_s']}",
    "--random-wait",
    f"--limit-rate={cfg['rate_limit']}",
    f"--tries={cfg['tries']}",
    f"--timeout={cfg['timeout_s']}",
    "--waitretry=10",
    # ── container hygiene ──────────────────────────────────────────
    "--hsts-file",
    str(tmp / ".wget-hsts"),  # else it writes to $HOME
    "--no-verbose",
    "--output-file",
    str(out / "crawl.log"),
    # Seeds go here OR positionally, never both — see the warning below.
    "--input-file",
    str(job_dir / "seeds.txt"),
]

if scope["reject_patterns"]:
    argv += ["--regex-type=pcre", "--reject-regex", "|".join(scope["reject_patterns"])]
if scope["accept_patterns"]:
    argv += ["--accept-regex", "|".join(scope["accept_patterns"])]
if excluded_hosts:
    argv += [f"--exclude-domains={','.join(excluded_hosts)}"]
if scope.get("path_prefix"):
    argv += ["--no-parent"]
if not scope["obey_robots"]:
    argv += ["-e", "robots=off"]
if scope.get("max_bytes"):
    argv += [f"--quota={scope['max_bytes']}"]
if auth.get("cookies_file"):
    argv += ["--load-cookies", auth["cookies_file"], "--keep-session-cookies"]
if cfg["content_on_error"]:
    argv += ["--content-on-error"]
if inc.get("dedup_cdx"):
    argv += ["--warc-dedup", inc["dedup_cdx"]]
if cfg["keep_mirror"]:
    argv += ["--directory-prefix", str(out / "files")]
else:
    argv += ["--delete-after"]  # WARC is the source of truth

argv += ["--user-agent", ua]
for k, v in auth.get("headers", {}).items():
    argv += ["--header", f"{k}: {v}"]

# NOT `argv += seeds` when --input-file is already present.
```

**Never build this as a shell string.** `subprocess` with an argv list and `shell=False`, always. URLs, hostnames, and regexes are user-controlled and go straight into this command.

**Seeds go through exactly one channel.** An earlier version of this document passed `--input-file` *and* appended the seeds positionally. wget does not de-duplicate across the two: it queues the URL twice and crawls the entire site a second time, for double the duration and roughly double the WARC, with no warning and exit code 0. The first real end-to-end capture did exactly that — the tell was 11 URL records for 5 unique pages. Use the seed file when it exists, positional arguments otherwise.

### `--delete-after` is incompatible with a seed list

The mirror on disk is not just output — it is how wget remembers which URLs it already has. `--delete-after` removes each file the instant it is written, and every subsequent seed then rediscovers the whole site as new.

With one seed this is invisible. With a seed list it is quadratic in the size of the blog, and nothing in the log says so. Measured on wget 1.25.0 against a six-seed site whose correct result is eight records:

| | records | distinct | ratio | orphan page found |
|---|---:|---:|---:|:-:|
| one seed, recursive | 7 | 7 | 1.0× | ✗ — unreachable by links |
| all seeds, `--delete-after` | 38 | 8 | 4.8× | ✓ |
| all seeds, keep the files | **8** | **8** | **1.0×** | ✓ |
| all seeds, `--no-recursion` | 0 | 0 | — | ✗ |

The orphan is a page listed in the sitemap but linked from nowhere — the case seed injection exists to cover. Only the third row gets both properties, so the engine always passes `--directory-prefix` and never `--delete-after`. `--no-clobber` and `--timestamping` make no difference; the mirror itself is the mechanism.

When the user has not asked to keep the mirror it is written into the job's temp directory and removed with it, so the cost is transient disk during the crawl rather than permanent storage. That is a real cost — roughly the uncompressed size of what is being archived — and it is the price of a crawl that reaches pages no chain of links leads to.

Failed requests are the one exception: a 404 writes no file, so it leaves no dedup record and a later seed may retry it. Bounded, cheap, and arguably correct, since a failure can be transient.

### Flag notes worth knowing

| Flag | Why it matters |
|---|---|
| `--warc-file` | Do *not* include `.warc.gz` — wget appends the extension and segment number itself |
| `--warc-tempdir` | Must be on the same filesystem as the output, or every segment close is a full byte copy |
| `--warc-dedup=FILE` | Reads a CDX from previous runs and emits `revisit` records instead of re-storing identical payloads. This is what makes incremental feed captures cheap. Behavior differs across wget versions — pin 1.21+. **Feed it every prior capture's CDX, not the last one** — see below |
| `--warc-cdx` | Produces the CDX that later runs' `--warc-dedup` consume. Not the replay index ([D11](00-decisions.md#d11--cdxj-for-replay-wgets-cdx-only-for-dedup)). **Writes nothing for a deduplicated URL** — see below |
| `--delete-after` | **Do not use.** See below — it silently multiplies the crawl |
| `--keep-session-cookies` | Blogger interstitial cookies are frequently session cookies. Without this they're dropped and the bypass silently fails |
| `--content-on-error` | Archives 4xx/5xx response bodies. Often the only record of a page that broke |
| `--hsts-file` | wget writes `~/.wget-hsts` by default; in a container with a read-only or shifting `$HOME` that's a hard failure |
| `--regex-type=pcre` | Required — POSIX ERE has no lookahead. Verified working on Debian's wget 1.25.0, whose banner reports neither `+pcre` nor `-pcre` (that flag described PCRE1; Debian links PCRE2 silently). The image checks this by compiling a real pattern, not by grepping the banner |
| `-N` / `--mirror` | **Avoid.** Timestamping interacts badly with WARC output and recursive re-crawls. Use `--warc-dedup` for incrementality instead |
| `--convert-links` | Pointless with `--delete-after`, and it never affects WARC records (which are always raw). Only relevant if keeping the mirror |

### Known wget limitations to document in the UI

- **It does not decode CSS escape sequences in `url(...)`.** Blogger skins write theme images as `url(https\:\/\/themes.googleusercontent.com\/image?id=…)`. A browser unescapes `\:` and `\/` and fetches the absolute URL; wget takes the literal string, finds no scheme, treats it as *relative*, and requests it against the blog. The result is a 404 per page the reference appears on, at paths like `/2026/08/https%5C:%5C/%5C/themes.googleusercontent.com%5C/image?id=…`, and the real image is never fetched.

  Confirmed on 1.25.0, in both `<style>` blocks and `src` attributes; an unescaped `url()` on the same page resolves correctly. Page content is unaffected — it costs a theme background. The `asset-audit` post-processor decodes the escapes, recovers the intended host, and reports it, because two 404s against your own domain with a percent-encoded backslash in them explain nothing on their own.

  Two things follow, and both were got wrong on the first attempt.

  **Reject the shape, in both spellings.** Every generated reject regex carries `\\|%5[Cc]` unconditionally. A backslash is never part of a real URL, so nothing legitimate is lost, and the requests are pure waste: one per referencing page per variant. The subtlety is that **wget prints and stores `%5C` but tests `--reject-regex` while the backslash is still literal**, so a pattern written from `crawl.log` alone never fires. Measured on 1.21.4 with `--debug`:

  | `--reject-regex` | requests | mangled GET | rule fires |
  |---|--:|:-:|:-:|
  | none | 3 | yes | — |
  | `%5[Cc]` | 3 | yes | no |
  | `\\` | 2 | no | yes |

  A live Blogger capture made 36 of these requests for six theme-image URLs, and the archived 404 bodies then got counted as pages by the audit — which is how a capture of four pages reported "16 page(s)".

  **Hand over the decoded URL.** Rejecting the mangled request stops the waste but does not archive the image; nothing wget can see ever names the real URL. Discovery decodes the escapes correctly, so it records those assets separately and the capture injects them into the seed file (docs/04). On the same blog that turned five missing skin images into five captured ones.

- **Memory grows with crawl size.** wget keeps the visited-URL set and WARC dedup index in memory. A 100k-URL crawl can reach several GB. Cap `max_pages` for very large sites, or split into path-scoped captures.
- **No JavaScript.** Lazy-loaded images (`data-src`) are missed. The engine should scan captured HTML for lazy-load attributes and emit a `warning` with a count — "312 images may be lazy-loaded and were not captured; consider the browser engine" — rather than leaving the user to discover the gaps during replay.
- **No infinite scroll / dynamic pagination.**
- **Single-threaded.** `wget2` is the drop-in upgrade for speed.

### Log parsing

wget's `--no-verbose` log lines look like:

```
2026-08-09 14:25:33 URL:https://example.blogspot.com/ [48213/48213] -> "…" [1]
https://example.blogspot.com/missing:
2026-08-09 14:25:35 ERROR 404: Not Found.
```

Prefer wget's own CDX output (`--warc-cdx`) over log scraping — it carries status, MIME, digest and offset in a stable format, whereas the human-readable log has drifted between versions.

**The CDX can be read incrementally.** Confirmed on 1.25.0: wget appends one line per archived record as it goes, in lockstep with the log, rather than flushing at the end. So `url` events can stream with real metadata during the crawl instead of being reconciled afterwards. Its exact shape, which the parser is written against:

```
 CDX a b a m s k r M V g u
http://example.com/i.html 20260810203702 http://example.com/i.html text/html 200 \
  ZHZVNSD7CHQFFQFY74ZTUDD5DD3YBW77 - - 898 /out/part-00000.warc.gz <urn:uuid:98b0…>
```

Eleven fields, **space**-separated (not tab), `-` for absent values, in order: url, timestamp, url again, MIME, status, payload digest (base32 SHA-1), redirect target, meta, compressed offset, filename, record id. The same URL legitimately appears more than once — a redirect target that is also fetched directly — so the consumer must tolerate repeats.

The two streams divide cleanly — almost. The CDX has everything that became a *response* record; the log has the failures that never became a record at all (connection refused, DNS, timeouts), which are invisible in the CDX and are exactly the ones worth showing a user.

**The exception, and it is a large one: a deduplicated URL appears in neither.** Measured on 1.25.0 — a second crawl of a four-page site with `--warc-dedup` wrote four `revisit` records into the WARC and a CDX containing **nothing but its header line**, while the crawl log listed all four `URL:` lines normally. Two consequences, both invisible until an incremental capture is examined:

- Built from the CDX alone, an incremental capture reports **zero URLs** and an empty URL list while its WARC is full. That reads as "the capture did nothing" at precisely the moment it did the best possible thing.
- The next run's `--warc-dedup` cannot be pointed at that CDX, because it is empty. Chaining capture to capture makes the saving hold for exactly one run and then silently alternate on and off. The dedup file must be the union of *every* prior capture's CDX, keyed on URL plus payload digest.

So the log is not only for failures. At the end of a crawl the engine reconciles its `URL:` lines against the CDX and emits the difference as `url` events with `revisit: true`, which is what makes `capture_urls` describe what wget actually fetched.

**With `--warc-max-size` set**, segments are `part-00000.warc.gz`, `part-00001.warc.gz`, … plus a `part-meta.warc.gz`, but there is still exactly one `part.cdx` covering all of them.

**A failing wget writes nothing to stderr** when `--output-file` is in use — everything, including the fatal error, goes to that file. A failure therefore surfaces as a bare exit code unless the engine reads the tail of `crawl.log` into its `result` message. "Could not open temporary WARC manifest file" and "Invalid regular expression" both live only there.

---

## Post-processors

The second addon type. Same manifest and protocol, different hook point — they run after a capture completes and receive the capture directory instead of a scope.

```yaml
apiVersion: cairn.postprocessor/v1
id: cdxj-index
name: "CDXJ indexer"
hook: after_capture
order: 10                     # lower runs first
required: true                # failure fails the capture
runtime:
  type: subprocess
  command: ["python", "-m", "cairn.post.cdxj"]
```

Built-in chain (✅ = shipped):

| Order | ID | Does | Required | |
|---:|---|---|:-:|:-:|
| 10 | `cdxj-index` | Builds `index/site.cdxj` across all of the site's WARCs | ✓ | M3 |
| 20 | `checksum` | SHA-256 every artifact, write into `manifest.json` | ✓ | ✅ |
| 30 | `stats` | Roll up counts and sizes onto the site row | ✓ | ✅ |
| 35 | `manifest` | Write `manifest.json` itself | ✓ | ✅ |
| 40 | `pywb-collection` | Regenerate pywb config, reload the collection | ✓ | M3 |
| 50 | `symlink-tree` | Refresh `/data/by-tag` (debounced) | | M4 |
| 60 | `asset-audit` | Report referenced-but-uncaptured assets and lazy-load hints | | ✅ |
| 50 | `text-extract` | Extract readable text into `derived/text/` and index it for search | | ✅ |
| 70 | `media` | `yt-dlp` the video an archived post embedded, per-site opt-in | | ✅ |
| 70 | `screenshot` | Homepage thumbnail for the site card (needs browser) | | ✗ |
| 80 | `wacz-export` | ~~Package as `.wacz` if the site opts in~~ — an export job, not a step. See below | | ✗ |
| 90 | `notify` | ntfy / Apprise / webhook on completion or failure | | M6 |

**The shipped chain runs in-process, not as subprocesses.** The manifest, the ordering and the required/optional distinction are all real; the isolation is not, because the built-ins need none and a subprocess contract nobody has written a second implementation of is a contract that will turn out to be wrong. It becomes a real addon boundary in M7, alongside the engine SDK, for the same reason engines got the seam first ([D3](00-decisions.md#d3--wget-for-v1-behind-an-engine-interface-from-day-one)).

**`wacz-export` is not a post-processor and should not be.** A WACZ is a whole-site package, so running it per capture would repackage every WARC the site has after every incremental capture of a few hundred kilobytes — the cost grows with the archive while the new content does not. It is an on-demand job instead (`POST /api/sites/{id}/export/wacz`).

**`media` is last and never required.** It reaches hosts outside the site's scope — that is what an embed is — and can take longer than the crawl did, so it is off unless a site asks for it and bounded per item, per capture and by count. Its URLs come out of archived HTML rather than from anything the user typed, which makes them the one genuinely attacker-controlled fetch target here, so they are checked against the private ranges docs/11 lists before anything connects.

**The manifest is rewritten after the chain finishes**, not only when something went wrong. It is written at order 35 so a chain that dies partway still leaves one, but every step after that — the index record count, the extracted text, the audit, the media — produces stats the first write cannot have seen. Rewriting only on warnings, which is what this did until M8, meant a capture that went perfectly recorded *less* on disk than one that did not.

`text-extract` runs at 50, after the index and before the asset audit, and does its own indexing rather than handing pages to a later step: the pages are in memory at that point and writing them out only to read them back would double the work for nothing. It is not required — a capture whose text could not be extracted is still a complete archive, and `Rebuild search index` regenerates it in seconds.

`checksum` computes the hash itself rather than recording what the engine claimed — otherwise the weekly integrity job verifies the engine's memory instead of the archive. Artifact paths are resolved against the capture directory with symlinks followed, and anything escaping it is refused: engine output is data, not instruction.

---

## Candidate engines beyond wget

Ranked by value. Details and alternatives in [14](14-tooling-landscape.md).

### 1. `browsertrix-crawler` — the JavaScript answer ✅ **built in M7**

Single Docker image, Chromium-based, purpose-built for archiving. Handles lazy-load and infinite scroll via *behaviors* (autoscroll, auto-play, site-specific scripts), supports `--profile` for pre-authenticated browser profiles, outputs WARC and WACZ.

This is the most valuable second engine because it covers every wget limitation at once. Runs as a sibling container, needs `--shm-size=2g`.

> **Its profile system is not a home for the `interactive` access-profile mode, and cannot be.** `crawl --help` has no cookie option at all; `--profile` takes a tar.gz of a browser profile directory. A profile built with our own Chromium is *accepted and ignored* — browsertrix runs **Brave** and cairn ships **Google Chrome for Testing**, so the cookie-encryption key differs. Verified end to end against a gated fixture: the crawl archived the interstitial. The engine declares `auth: [user_agent]` and warns before any capture of a site that has an access profile. A gated site wants the wget engine, or a custom behavior that clicks through the warning.
>
> **Leave `--behaviors` at its default.** It is `autoplay,autofetch,autoscroll,siteSpecific`, and passing a shorter list drops `autofetch` — the behavior that fetches lazily-referenced resources, which is most of the reason to use this engine. Measured: the lazy-image fixture went from three images to one.
>
> **Its output layout is not cairn's.** It writes `collections/<name>/archive/*.warc.gz`; a capture keeps WARCs in `warc/`, which is what `site_warcs` globs and therefore all that ever reaches replay. The adapter moves them.
>
> **Its stdout has no per-URL record.** Pages started and finished, and a periodic `crawlStatus` — the complete list of what was archived is only in the CDXJ it writes at the end, which is where the `url` events come from.
>
> **Do not override the container's working directory.** Its Dockerfile sets `WORKDIR /crawls` and it resolves its output tree from there, so pointing it elsewhere wrote the crawl where nobody was looking — and still exited 0, reporting two pages crawled and no archive.

### 2. `single-file-cli` — the one-file snapshot

Produces a single self-contained HTML file with everything inlined. Not WARC, so not replay-through-pywb, but perfect for "just keep this one page forever" and trivially portable. Good as a *supplementary* engine that runs alongside the primary one on selected pages.

### 3. `yt-dlp` — embedded media

Blogs embed YouTube and Vimeo. Neither wget nor a browser crawler captures the actual video stream. A post-processor that scans captured HTML for embeds and offers per-site opt-in media capture into `derived/media/` closes a real gap — and it's the gap people notice years later.

### 4. `wget2` — the speed upgrade

Multi-threaded, HTTP/2, WARC support. Near drop-in replacement. Flag names differ slightly, so it's a separate engine rather than a config toggle.

### 5. `warcprox` — record anything

A MITM recording proxy. Point any HTTP client — a headless browser, a custom script, even your own desktop browser — through it and everything gets WARC'd. The most flexible option and the escape hatch for sites nothing else handles. Requires CA certificate handling, which is a UI/UX cost.

---

## Writing an addon: the short version

```bash
cp -r examples/engine-template /config/engines/my-engine
# edit engine.yaml — `id` must match the directory name
cairn engines validate /config/engines/my-engine
cairn engines test     /config/engines/my-engine
```

Then **Rescan engines** in Settings, or restart. The engine appears in the site editor with a form generated from your schema.

`examples/engine-template/` is a working engine in two files, with its own README. `cairn engines test` runs it against a fixture site the harness serves itself and checks every rule core relies on — including the ones core enforces silently, which are exactly the ones an addon author never discovers until a capture behaves strangely six months later:

| Check | Why it is not left to good intentions |
|---|---|
| stdout is NDJSON | A stray `print()` is survivable — core counts and skips malformed lines — but it is still a bug |
| emits `started` | It is the first line of the live log |
| emits `url` events | Without them a capture has no URL list and no gap report |
| exactly one `result` | No result is a failure whatever the exit code: an engine that stopped without saying how it went is indistinguishable from one that crashed |
| the exit code agrees | `result: ok` followed by exit 1 does not get to be ok |
| artifacts inside `output_dir` | Engine output is data, not instruction; enough `..` would have core checksum, and later serve, a file anywhere on disk |
| artifacts exist | A declared file that is not there fails the checksum step, not the capture |

**Capabilities are a promise, not decoration.** The UI hides options an engine cannot honour and warns when a site needs something it does not declare. The shipped browsertrix engine declares `auth: [user_agent]` for exactly this reason — claiming `cookies` it then ignores would produce an archive full of content warnings, with nothing anywhere saying why.

The addon system is only real if someone other than its author can use it, and the conformance test is what makes that true.
