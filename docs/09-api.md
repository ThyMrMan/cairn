# 09 — API Surface

FastAPI, JSON, OpenAPI at `/api/openapi.json`. The frontend consumes a client generated from that schema, so the API is the contract and types flow from the Pydantic models outward.

Conventions:
- All endpoints under `/api`, session-cookie authenticated except `/api/auth/*` and `/api/health`.
- Mutating requests require `X-Requested-With: XMLHttpRequest` in addition to `SameSite=Lax` cookies ([11](11-security.md#csrf)).
- Lists are paginated: `?page=1&per_page=50`, envelope `{items, total, page, per_page}`.
- Errors: `{"error": {"code": "scope_invalid", "message": "…", "detail": {…}}}` — stable machine-readable `code`, human `message`.
- Timestamps are UTC ISO 8601 with `Z`.
- Long operations return `202` with `{"job_id": N}`; progress comes over SSE.
- **`GET` requires no CSRF header** — only mutating methods do, so a read endpoint can be opened straight from a browser address bar while logged in. Worth knowing when diagnosing something by hand.
- **`?q=` is a literal substring, not a pattern.** `%` and `_` are SQL `LIKE` wildcards and are escaped, so searching `100%` finds the rows containing a percent sign rather than every row in the table. They were not escaped until it was noticed making three different URL-pattern counts each come back as the size of the whole archive.

---

## Auth

| Method | Path | Notes |
|---|---|---|
| `POST` | `/api/auth/login` | `{username, password, totp?}` → sets session cookie. Rate limited, constant-time-ish failure |
| `POST` | `/api/auth/logout` | Revokes the current session |
| `GET` | `/api/auth/me` | Current user + whether 2FA is enabled |
| `POST` | `/api/auth/password` | `{current, new}` — revokes all other sessions on success |
| `POST` | `/api/auth/totp/setup` | → provisioning URI + QR payload |
| `POST` | `/api/auth/totp/confirm` | `{code}` — activates 2FA |
| `DELETE` | `/api/auth/totp` | `{password, code}` — requires reauth |
| `GET` | `/api/auth/sessions` | Active sessions with UA/IP/last-seen |
| `DELETE` | `/api/auth/sessions/{id}` | Revoke one |

Login failures return one generic message regardless of cause. Never distinguish "no such user" from "wrong password" from "locked".

---

## Folders & tags

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/folders` | Full tree with per-folder site counts and rolled-up sizes |
| `POST` | `/api/folders` | `{name, parent_id?}` — creates the directory too |
| `PATCH` | `/api/folders/{id}` | Rename, reparent (`{parent_id, reparent: true}`) or reorder → `MoveOutcome` |
| `DELETE` | `/api/folders/{id}` | `409` if it holds anything, unless `?reassign_to=<id>` |
| `GET` | `/api/tags` | With usage counts |
| `POST` `PATCH` `DELETE` | `/api/tags[/{id}]` | Name, colour, description |
| `GET` `POST` `PATCH` `DELETE` | `/api/views[/{id}]` | Saved smart views |

### `MoveOutcome`, and why moves are not always `202`

Every endpoint that changes a path returns the same shape:

```json
{"status": "done", "method": "rename", "path": "Weblogs/Photography", "job_id": null}
```

This document originally specified `202` for a folder change, on the assumption that moving files is slow. It is not: inside one filesystem a move is one `rename(2)` and completes before the response is written, whatever the directory holds. Returning `202` for that would mean showing a progress bar for an operation that already finished, and polling a job that is already `ok`.

The slow case is real but rare — the two ends on different filesystems, which on Unraid means `/data` spanning array disks. There the move is a byte copy, and it becomes a `move` job: `status: "queued"` with a `job_id` to watch on the existing jobs stream. The client cannot predict which it will get, so the server says.

`PATCH /api/sites/{id}` with a changed `folder_id` takes the fast path only. If that folder turns out to be on another filesystem it returns `409 cross_device` pointing at `POST /api/sites/{id}/move`, rather than silently starting a ten-minute copy inside what looked like a metadata edit.

Reparenting needs `reparent: true` alongside `parent_id`, because JSON cannot otherwise distinguish "leave the parent alone" from "move it to the top level" — both would arrive as a missing or null field.

---

## Sites

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/sites` | Filter/sort — see below |
| `POST` | `/api/sites` | `{seed_url, title?, folder_id, tags?, profile_id?}` → creates and optionally auto-runs discovery |
| `GET` | `/api/sites/{id}` | Full detail with scope, feeds, latest captures, stats |
| `PATCH` | `/api/sites/{id}` | Title, notes, folder, tags, engine, engine config, profile, `keep_mirror` |
| `DELETE` | `/api/sites/{id}` | Soft delete → `trash/`; `?purge=true` for immediate |
| `POST` | `/api/sites/{id}/restore` | Undelete from trash |
| `POST` | `/api/sites/{id}/move` | `{folder_id}` → `MoveOutcome` |
| `POST` | `/api/sites/bulk` | `{site_ids, add_tags?, remove_tags?, folder_id?}` |
| ~~`GET`~~ | ~~`/api/sites/{id}/urls`~~ | **Never existed.** Captured URLs are per capture — see `/api/captures/{id}/urls` |
| `GET` | `/api/sites/{id}/stats` | Sizes, counts, capture history, growth over time |

### Filtering

`GET /api/sites` accepts:

```
folder_id, folder_recursive=true
tag[]=travel&tag[]=photography&tag_mode=all|any
status, engine_id, profile_id, host
has_errors, has_feeds, never_captured
last_capture_before, last_capture_after
size_min, size_max
q                       # substring over title, seed_url, host, notes
sort                    # title|created_at|last_capture_at|size_bytes|url_count (prefix - to reverse)
```

The same filter object serializes into `saved_views.query`, so a saved smart view is literally a stored query string. Keep them identical — a divergence between "what the filter bar produces" and "what a saved view stores" is a bug factory.

**As built:** one `SiteFilter` class does all four jobs — reads query parameters, writes them back, reads stored JSON, and compiles to SQL — and `GET /api/sites` reads its filter off the raw query string rather than declaring parameters, because `tag` repeats and because declaring them twice is the divergence this warns about. Saving a view round-trips the query through the same object, so a stored filter that no longer parses fails once, when it is saved or listed, instead of silently matching everything.

Two details that fall out of it:

- **Only non-default fields are serialized.** A view saved today carries no opinion about a field added next year, rather than an accidental `false` that would quietly narrow it.
- **`has_errors` and `never_captured` have three states.** `has_errors=false` means "only sites with clean captures", which is not the same as not filtering on it — so absent, true and false are all distinct.

Anything a filter can express must survive a round trip through both serializations unchanged. That is asserted directly in `test_filters.py` rather than left as a thing to remember.

---

## Discovery & scope

| Method | Path | Notes |
|---|---|---|
| `POST` | `/api/sites/{id}/discover` | `?max_pages=&max_depth=&use_browser=` → `202 {job_id}` |
| `GET` | `/api/sites/{id}/discoveries` | History |
| `GET` | `/api/discoveries/{id}` | Full result: hosts, feeds, sitemaps, platform fingerprint |
| `GET` | `/api/discoveries/{id}/diff?against={id}` | New/removed hosts and URLs |
| `GET` | `/api/sites/{id}/seeds` | Every address this site starts from, primary first |
| `POST` | `/api/sites/{id}/seeds` | `{url}` → adds a seed and makes its host crawlable |
| `DELETE` | `/api/sites/{id}/seeds?url=` | Removes one; the first seed cannot go |
| `GET` | `/api/sites/{id}/reader?url=&capture=` | One archived page as clean text |
| `GET` | `/api/sites/{id}/reader/versions?url=` | Captures whose text holds that URL |
| `GET` | `/api/sites/{id}/reader/index` | Every readable page of a site |
| `GET` | `/api/sites/{id}/annotations?url=` | Notes on one page, or all of a site's |
| `POST` | `/api/sites/{id}/annotations` | `{url, quote, note?, prefix?, suffix?, block_index?}` |
| `PATCH` | `/api/annotations/{id}` | Edit the note or its colour |
| `DELETE` | `/api/annotations/{id}` | Remove one |
| `GET` | `/api/sites/{id}/scope` | Current resolved scope |
| `PUT` | `/api/sites/{id}/scope` | Host selections, patterns, limits, politeness |
| `POST` | `/api/sites/{id}/scope/preview` | Dry-run estimate — no fetching |
| `GET` | `/api/presets` | Platform presets (blogger, wordpress, …) |
| `POST` | `/api/sites/{id}/scope/apply-preset` | `{preset: "blogger"}` |
| `GET` | `/api/crawl/skip-patterns` | `{patterns}` — the skip list every site inherits |
| `PUT` | `/api/crawl/skip-patterns` | `{patterns}`, whole list. `422 invalid_pattern` if one will not compile |

The scope response keeps three pattern lists apart and they are not
interchangeable. `reject_patterns` is the site's own.
`global_reject_patterns` is the entire instance-wide list, including entries
this site has switched off — the picker has to show a rule in order to offer
turning it back on. `global_reject_exceptions` names the ones currently off
here, and is the only one of the three the `PUT` accepts alongside the site's
own.

The `PUT` on the global list replaces it wholesale rather than offering
add/remove: every pattern has to be recompiled anyway to know the result is
runnable, and a partial update lets two clients each succeed and leave a list
neither asked for. Removing a pattern removes it from every site at once —
see [04](04-discovery-and-scoping.md#the-skip-list-that-applies-to-every-site)
for why it is merged at resolve time rather than copied into sites.

---

## Captures

| Method | Path | Notes |
|---|---|---|
| `POST` | `/api/sites/{id}/capture` | `{kind: full\|incremental, seeds?, engine_id?, config_override?}` → `202 {job_id}` |
| `GET` | `/api/sites/{id}/captures` | List |
| `GET` | `/api/captures/{id}` | Manifest, WARC files, stats |
| `DELETE` | `/api/captures/{id}` | `409` if it's the only capture unless `?force=true`; triggers reindex |
| `GET` | `/api/captures/{id}/log` | Plain text; `?tail=500` |
| `GET` | `/api/captures/{id}/urls` | With `?errors_only=true`, `?host=`, `?q=` |
| `GET` | `/api/captures/{id}/url-shapes` | What the capture is fetching, grouped by URL shape, biggest first. Works mid-crawl |
| `POST` | `/api/captures/{id}/retry-failed` | New capture seeded from this one's failures |
| `POST` | `/api/captures/{id}/reindex` | Rebuild the site index |
| `POST` | `/api/captures/{id}/export/wacz` | `202 {job_id}` — this capture alone |

### Embedded media

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/sites/{id}/media` | Effective policy, whether `yt-dlp` is present, and every item recent captures downloaded **or refused, with the reason** |
| `PUT` | `/api/sites/{id}/media` | `{enabled?, max_item_bytes?, max_total_bytes?, max_items?, format?, allow_private_hosts?}`. An empty body clears the site's override and returns it to the instance default |
| `GET` | `/api/sites/{id}/media/{capture_dir}/{filename}` | The file itself, answering Range requests so it can be played in place |
| `GET` | `/api/media/settings` | The instance-wide default every site inherits |
| `PUT` | `/api/media/settings` | Same body. An empty one restores the built-in defaults |

`policy` comes back merged — built-in under the `media.download` instance
setting under the site's own override — because a form that edits one layer
while displaying another is a form that lies. The per-site response also
carries `instance` and `override` separately, so the UI can say which layer a
value came from and offer a way back to the one underneath.

**Setting the instance default does not switch media on across an existing
archive.** A site that has been given an explicit answer keeps it; the default
decides what a site gets when nobody has said otherwise, which in practice is
every site added afterwards.

The file endpoint serves **inline**, unlike a WACZ export, because a video
nobody can play is not much of an archive. What makes that safe is that the
type is never inferred: a short extension allowlist maps to one fixed content
type and everything else is a 404, with `nosniff` and a `sandbox` CSP on top.
yt-dlp takes the extension from the remote server, so it is precisely the sort
of attacker-influenced string that must not be allowed to pick its own type.

---

## Jobs & events

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/jobs` | `?status=&type=&site_id=` |
| `GET` | `/api/jobs/{id}` | State, progress, error |
| `GET` | `/api/jobs/{id}/projection` | Rate, distance to `max_pages`, and the index's estimate for contrast. No percentage — see below |
| `POST` | `/api/jobs/{id}/cancel` | SIGTERM → grace → SIGKILL |
| `DELETE` | `/api/jobs/{id}` | Remove a finished job from the list. `409 job_is_active` if it is queued or running — cancel it first |
| `POST` | `/api/jobs/clear` | Bulk delete finished jobs. Optional `status`, `type`, `site_id`; nothing set clears every finished job. Returns `{deleted}`. Never touches a queued or running job, and `status: "running"` is a `422` rather than a silent `deleted: 0` |

> Clearing the job list does not touch archives. `captures.job_id`, `discoveries.job_id` and `feed_polls.job_id` are all `ON DELETE SET NULL`, so a capture outlives the job that made it and simply stops naming it.
| `POST` | `/api/jobs/{id}/resume` | For `interrupted` jobs |
| `GET` | `/api/jobs/{id}/events` | **SSE** |
| `GET` | `/api/events` | **SSE** — global firehose for the activity sidebar |

**`/projection` reports no percentage, and that is the design.** Nothing knows how many URLs a site has until the crawl has found them. A bar drawn against the index's page estimate would have read *370% complete* on the crawl that prompted this endpoint — worse than showing nothing, because it looks like an answer. What it returns instead is the rate, the distance to the site's own cap, and the index estimate beside the live count; "this is not converging" reads off those in a second. See [04](04-discovery-and-scoping.md#why-the-crawl-is-always-bigger-than-this-number).

### SSE

```
event: progress
data: {"job_id":512,"done":412,"total":1847,"bytes":183042110,"eta_s":4120}

event: log
data: {"job_id":512,"level":"info","msg":"…","ts":"2026-08-09T14:31:02Z"}

event: status
data: {"job_id":512,"status":"ok","stats":{"urls":1847,"errors":12}}
```

Heartbeat comment every 15 s so proxies don't idle out the connection. Client reconnects with `Last-Event-ID`; the server replays from a bounded in-memory ring buffer (last ~500 events per job) — a reconnecting log viewer that silently drops the events it missed is worse than one that says it did. When the gap is larger than the buffer the client gets a `lagged` event rather than a quietly incomplete log.

**The per-job stream ends when the job does.** It closes after the terminal `status` event, and a request for a job that already finished replays its history and then closes rather than waiting. Getting this wrong is easy and invisible: the first implementation replayed and then blocked forever, holding a connection and a server task open for every completed job anyone opened, with the client given no way to know nothing more was coming.

A stalled reader must never apply backpressure to a crawl, so each subscriber has a bounded queue and is marked lagged rather than waited on. `/api/events` has no terminal condition and stays open until the client leaves.

---

## Access profiles

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/profiles` | **Metadata only** — never secret material |
| `POST` | `/api/profiles` | `{name, mode, user_agent?, hosts?, verify_url?}` |
| `PATCH` | `/api/profiles/{id}` | Non-secret fields |
| `PUT` | `/api/profiles/{id}/cookies` | `multipart` upload of `cookies.txt`; write-only. Returns the parse report |
| `PUT` | `/api/profiles/{id}/script` | Upload `.user.js`; write-only. Returns parsed metadata + shim warnings |
| `PUT` | `/api/profiles/{id}/browser-profile` | `multipart` upload of a browsertrix `profile.tar.gz`; write-only. Returns size, digest and stored time |
| `DELETE` | `/api/profiles/{id}/browser-profile` | Remove the browser profile, leaving the jar alone |
| `DELETE` | `/api/profiles/{id}/material` | Clear stored secrets, including the browser profile on disk |
| `POST` | `/api/profiles/{id}/mint` | Run the script in a browser → `{result, profile}` |
| `POST` | `/api/profiles/{id}/verify` | Fetch `verify_url` with the jar → `{ok, reason, status, final_url}` |
| `GET` | `/api/profiles/{id}/coverage?site_id=N` | Which of a site's scope hosts the jar covers |
| `POST` | `/api/profiles/{id}/interactive` | Start a live browser session → `{session_id, url, width, height}` |
| `WS` | `/api/profiles/{id}/interactive/ws?session_id=` | JPEG frames out, mouse and keyboard in |
| `POST` | `/api/profiles/{id}/interactive/save` | Seal what the session collected |
| `DELETE` | `/api/profiles/{id}/interactive` | Close it without saving |

`/mint` and `/verify` are synchronous rather than the `202 {job_id}` this table originally specified. Both are a single page load with a hard ceiling, and the person who pressed the button is waiting to find out whether their script worked — watching a job row for something that takes five seconds is worse in every way.

`/verify` deliberately uses plain HTTP rather than the browser. The question it answers is whether *wget* will get real content, and a browser would answer a different one: it runs the site's JavaScript and can talk its way past a gate that wget then cannot, which is the exact failure the check exists to catch.

The interactive session is a **CDP screencast over a WebSocket**, not the `vnc_url` this table used to name — see [06](06-access-profiles.md#a-cdp-screencast-not-novnc) for why the whole VNC stack turned out to be unnecessary.

**That socket carries its own protections, because it inherits none of the API's.** The same-origin policy does not apply to WebSockets and a handshake carries no CSRF header, so any page could otherwise open a socket to a LAN address, have the browser attach the session cookie, and receive a driveable browser looking at whatever the user is signed into. `Origin` is checked during the handshake, and `connect-src` names the socket's origin explicitly rather than relying on `'self'` to cover `ws:`.

---

## Feeds

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/sites/{id}/feeds` | Each row carries its poll state and its item counts |
| `POST` | `/api/sites/{id}/feeds` | `{url, kind?, title?, interval_min?, enabled?, auto_capture?}` |
| `POST` | `/api/sites/{id}/feeds/discover` | Everything worth watching, probed live. Saves nothing |
| `POST` | `/api/sites/{id}/feeds/test` | `{url, kind?}` — parse without saving; returns format, entry count, recent titles, scope check |
| `PATCH` `DELETE` | `/api/feeds/{id}` | |
| `POST` | `/api/feeds/{id}/poll` | Poll now, synchronously, and capture what it finds |
| `POST` | `/api/feeds/{id}/capture` | Capture what is already pending, without polling |
| `GET` | `/api/feeds/{id}/items` | `?status=pending\|captured\|failed\|skipped` |
| `GET` | `/api/feeds/{id}/polls` | Poll history: status, entries, new items, action, error |

**Test is per site, not global.** This document had `POST /api/feeds/test` with no site in the path, which cannot answer the question that makes the endpoint worth having: whether the feed's entries are inside *this site's* scope. A feed whose entries fall outside it polls happily forever, finds new posts every time, and archives none of them.

**Poll is synchronous, not `202`.** A poll is one conditional GET and it finishes in well under a second; handing back a job id would mean a progress bar for work already done, and the response can carry what it actually found. The capture it enqueues *is* a job, and its ids come back in `job_ids`.

**There is no `capture-pending`; it is `capture`.** Same operation, and the noun was already unambiguous under `/feeds/{id}/`.

### Scheduling & notifications

| Method | Path | Notes |
|---|---|---|
| `GET` `PUT` | `/api/schedule` | Quiet hours, per-host serialization, full-recapture interval |
| `GET` `PUT` | `/api/notifications` | Targets and the per-event opt-ins |
| `POST` | `/api/notifications/test` | One message to every enabled target; reports per-target failures |

---

## Engines & post-processors

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/engines` | Installed, with capabilities, plus `available` and `unavailable_reason` |
| `GET` | `/api/engines/{id}/schema` | JSON Schema and defaults for the config form |
| `POST` | `/api/engines/rescan` | Re-read `/config/engines` |
| `POST` | `/api/engines/{id}/validate` | Validate a config object without saving |

**`enabled` and `available` are different questions.** The first is whether the manifest loaded; the second is whether this host can run it. A container engine on a machine with no Docker socket is a perfectly valid engine that cannot run, and the picker has to be able to say which — otherwise it is selectable and fails at capture time.

**There is no `PATCH /api/engines/{id}`.** This document listed one for enable/disable and instance defaults, and neither turned out to be a real thing: an engine is enabled by being installed and disabled by being removed, and "instance defaults" would be a third layer under the schema defaults and the per-site config with no case that needs it. An engine is chosen and configured per site, through `PATCH /api/sites/{id}`.
| `GET` | `/api/postprocessors` | Chain with order and enabled state |
| `PATCH` | `/api/postprocessors/{id}` | Enable/disable, reorder |

---

## System

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/health` | Unauthenticated. `{status, version, db, pywb, disk_free_bytes}` |
| `GET` | `/api/version` | **Authenticated.** `{version, build, built_at, label}` — which build is running |
| `GET` | `/api/settings` | All DB-backed settings |
| `PATCH` | `/api/settings` | Partial update |
| `GET` | `/api/storage` | Per-folder and per-site usage, free space, trash size |
| `GET` | `/api/digest?days=` | The periodic report: what happened, and what quietly did not |
| `GET` | `/api/site-health` | Which archived sites are still live, and which are not |
| `POST` | `/api/sites/{id}/health-check` | Probe one site's live counterpart now |
| `GET` | `/api/mirror?path=` | Which captures a mounted copy of /data has |
| `POST` | `/api/mirror/verify?path=&deep=` | Re-checksum the copy against what was recorded here |
| `POST` | `/api/import/urls/survey` | `{text}` → what a pasted list would become |
| `POST` | `/api/import/urls` | Create or reuse a site per domain and archive the listed pages |
| `GET` | `/api/trash` | Deleted sites, their size, and days until purge |
| `DELETE` | `/api/trash` | Purge everything in the trash now |
| `POST` | `/api/maintenance/verify` | `?site_id=&deep=` → `202 {job_id}`. `deep` also parses every WARC |
| `GET` | `/api/maintenance/integrity` | Archive health: verified count, oldest unverified capture, last report |
| `POST` | `/api/maintenance/reindex-search` | `?site_id=&extract=` → `202 {job_id}` |
| `GET` | `/api/sites/{id}/thumbnail` | The site card's picture of the archived front page, or 404. `image/jpeg`, `nosniff`, ETag |
| `GET` | `/api/thumbnails/settings` | `{enabled}` — whether a capture takes one |
| `PUT` | `/api/thumbnails/settings` | `{enabled}` |
| `POST` | `/api/maintenance/thumbnails` | `?site_id=&force=` → `202 {job_id}`. Backfills sites captured before thumbnails existed |
| `GET` | `/api/search` | `?q=&site_id=&folder=&tag=&limit=&offset=` → ranked hits with snippets |
| `GET` | `/api/search/status` | `{pages, words, sites, unindexed_sites}` |
| `GET` | `/api/sites/{id}/exports` | The site's `.wacz` files, from the directory |
| `POST` | `/api/sites/{id}/export/wacz` | `202 {job_id}` — every capture in one file |
| `GET` | `/api/sites/{id}/exports/{name}` | Download. Always an attachment |
| `GET` | `/api/sites/{id}/exports/{name}/verify` | Checksums, and every index entry resolving |
| `DELETE` | `/api/sites/{id}/exports/{name}` | Remove an export. The archive is untouched |
| `GET` | `/api/sites/{id}/diff` | `?before=&after=` — which pages two captures disagree about. Defaults to the last two |
| `GET` | `/api/sites/{id}/diff/page` | `?url=&before=&after=` — one page, block and word level |
| `GET` | `/api/sites/{id}/diff/resources` | Assets added, removed, or replaced under the same URL |
| `GET` | `/api/sites/{id}/retention` | The dry run: every capture, kept or not, and why |
| `PUT` | `/api/sites/{id}/retention` | `{enabled?, keep_last?, keep_monthly?, min_age_days?}` → the new plan |
| `POST` | `/api/sites/{id}/retention/apply` | `202 {job_id}`. Not reversible |
| `GET` | `/api/import/archivebox` | `?path=` — what is in that archive, without touching it |
| `POST` | `/api/import/archivebox` | `?path=&host=&folder_id=` → `202 {job_id}` |
| `GET` | `/api/metrics` | Prometheus exposition. **Unauthenticated**, off by default |
| `GET` | `/api/metrics/settings` | `{enabled, token_set}` — never the token itself |
| `PUT` | `/api/metrics/settings` | `{enabled?, token?}` |
| `POST` | `/api/maintenance/rebuild-symlinks` | Regenerate `/data/by-tag` |
| `POST` | `/api/maintenance/rebuild-collections` | Re-point every pywb collection |
| `POST` | `/api/maintenance/rebuild-db` | Reconstruct DB rows from on-disk manifests |
| `POST` | `/api/maintenance/recompute-status` | Re-decide `partial` captures from their manifests |
| `POST` | `/api/maintenance/purge-trash` | Purge only what is past the retention window |

`/api/search` never receives FTS5 syntax. What somebody types is translated: every bare term becomes a quoted string literal, and only `"a phrase"`, a trailing `*` and a leading `-` are read as operators. Typing `c++` or a stray quote is a MATCH syntax error otherwise, and `AND` is an operator rather than the word somebody meant — a search box that answers with *fts5: syntax error near* is a search box people stop using.

`/api/metrics` is the only authenticated-by-default endpoint that can be opened up, because a scraper cannot log in. What makes that acceptable is what it never contains: no site title, URL, host, folder or tag, only counts with fixed-vocabulary labels. `metrics.token` adds `Authorization: Bearer`, which is what Prometheus's `bearer_token` sends natively; reads report only *whether* a token is set, never its value.

`/api/sites/{id}/retention` is a dry run on every path, including when the policy is switched off — which is the state it most needs to be readable in, because the dry run is how somebody decides whether to switch it on. `apply` recomputes the plan inside the job rather than taking the one the browser is showing: a capture that has become the last copy of something in the meantime must not be deleted because an older plan said it could be, and the job reports each such refusal rather than silently skipping it.

`/api/sites/{id}/exports/{name}` is always `Content-Disposition: attachment` with `X-Content-Type-Options: nosniff`. A WACZ is a zip of untrusted archived bytes and the app origin is the last place it should ever be rendered ([11](11-security.md)).

`/api/storage` reports per-site totals from `sites.size_bytes`, measured by the `stats` post-processor at the end of each capture — not by walking the tree on request. On a NAS array that walk is thousands of cold `stat` calls and the page would take seconds while spinning up disks nobody asked to wake. Free space and trash size are measured live, being one `statvfs` and one directory that is normally small.
| `GET` | `/api/audit` | Auth and admin events |
| `GET` | `/api/export/config` | Full config backup as JSON — **excludes secret material** |
| `POST` | `/api/import/config` | Restore |

`/api/health` is unauthenticated because Unraid's healthcheck and any uptime monitor need it. It therefore must leak nothing: no version-specific vulnerability hints beyond the version string, no paths, no site names, no counts.

`/api/version` is the split from that. `version` alone is useless for the question people actually ask — it changes on a release and not on a commit, so an image several milestones behind reports exactly what a current one does. `build` is the part that changes, and because it names a commit it also names that commit's known bugs, so it sits behind a session while `/health` keeps the bare version it always had. Resolution order is `CAIRN_BUILD`/`CAIRN_BUILT_AT` in the environment, then the `BUILD_INFO` file the image writes, then `git describe` in a source checkout, then the literal string `source`. Nothing is invented: a fabricated id would read as a real build nobody can find.

---

## Replay endpoints

Served by pywb on the replay origin, not by this API:

```
{REPLAY_ORIGIN}/site-{id}/                                 collection root
{REPLAY_ORIGIN}/site-{id}/{timestamp}/{url}                specific capture
{REPLAY_ORIGIN}/site-{id}/*/{url}                          all versions
{REPLAY_ORIGIN}/site-{id}/cdx?url=…&output=json            CDX API — pywb's own
```

The chrome does **not** use that CDX API. It reads the app's own copy of the index instead:

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/sites/{id}/replay` | Collection name, record count, and the replay origin to frame |
| `GET` | `/api/sites/{id}/replay/versions?url=…` | Every capture of one URL — the capture selector's data |
| `GET` | `/api/sites/{id}/replay/record?url=…&timestamp=…` | Raw WARC record: headers and metadata as JSON |
| `GET` | `…/record?…&download=true` | The payload, always `application/octet-stream` as an `attachment` |
| `POST` | `/api/sites/{id}/reindex` | Rebuild the index from the WARCs |

Reading our own index rather than proxying pywb keeps the URL bar, the capture selector and the version count working when pywb is down — one failure then looks like one failure, instead of an empty frame with no explanation. It also means no replayed byte is ever served from the app's origin, which is the point of running pywb on its own.

`replay` returns the origin it computed from the request, and the CSP's `frame-src` is computed by the same function. If those two ever disagree the browser blocks the iframe and the only clue is in the console, so they share one implementation rather than two that look alike.
