# 09 — API Surface

FastAPI, JSON, OpenAPI at `/api/openapi.json`. The frontend consumes a client generated from that schema, so the API is the contract and types flow from the Pydantic models outward.

Conventions:
- All endpoints under `/api`, session-cookie authenticated except `/api/auth/*` and `/api/health`.
- Mutating requests require `X-Requested-With: XMLHttpRequest` in addition to `SameSite=Lax` cookies ([11](11-security.md#csrf)).
- Lists are paginated: `?page=1&per_page=50`, envelope `{items, total, page, per_page}`.
- Errors: `{"error": {"code": "scope_invalid", "message": "…", "detail": {…}}}` — stable machine-readable `code`, human `message`.
- Timestamps are UTC ISO 8601 with `Z`.
- Long operations return `202` with `{"job_id": N}`; progress comes over SSE.

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
| `POST` | `/api/folders` | `{parent_id, name}` |
| `PATCH` | `/api/folders/{id}` | Rename or reparent → `202` (moves files on disk) |
| `DELETE` | `/api/folders/{id}` | `409` if non-empty unless `?reassign_to=<id>` |
| `GET` | `/api/tags` | With usage counts |
| `POST` `PATCH` `DELETE` | `/api/tags[/{id}]` | Name, slug, color |

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
| `GET` | `/api/sites/{id}/urls` | Captured URLs; `?status=&mime=&host=&q=&errors_only=` |
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

---

## Discovery & scope

| Method | Path | Notes |
|---|---|---|
| `POST` | `/api/sites/{id}/discover` | `{max_pages?, max_depth?, obey_robots?}` → `202 {job_id}` |
| `GET` | `/api/sites/{id}/discoveries` | History |
| `GET` | `/api/discoveries/{id}` | Full result: hosts, feeds, sitemaps, platform fingerprint |
| `GET` | `/api/discoveries/{id}/diff?against={id}` | New/removed hosts and URLs |
| `GET` | `/api/sites/{id}/scope` | Current resolved scope |
| `PUT` | `/api/sites/{id}/scope` | Host selections, patterns, limits, politeness |
| `POST` | `/api/sites/{id}/scope/preview` | Dry-run estimate — no fetching |
| `GET` | `/api/presets` | Platform presets (blogger, wordpress, …) |
| `POST` | `/api/sites/{id}/scope/apply-preset` | `{preset: "blogger"}` |

---

## Captures

| Method | Path | Notes |
|---|---|---|
| `POST` | `/api/sites/{id}/capture` | `{kind: full\|incremental, seeds?, engine_id?, config_override?}` → `202 {job_id}` |
| `GET` | `/api/sites/{id}/captures` | List |
| `GET` | `/api/captures/{id}` | Manifest, WARC files, stats |
| `DELETE` | `/api/captures/{id}` | `409` if it's the only capture unless `?force=true`; triggers reindex |
| `GET` | `/api/captures/{id}/log` | Plain text; `?tail=500` |
| `GET` | `/api/captures/{id}/urls` | With `?errors_only=true` |
| `POST` | `/api/captures/{id}/retry-failed` | New capture seeded from this one's failures |
| `POST` | `/api/captures/{id}/reindex` | Rebuild the site index |
| `POST` | `/api/captures/{id}/export/wacz` | `202 {job_id}` |

---

## Jobs & events

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/jobs` | `?status=&type=&site_id=` |
| `GET` | `/api/jobs/{id}` | State, progress, error |
| `POST` | `/api/jobs/{id}/cancel` | SIGTERM → grace → SIGKILL |
| `POST` | `/api/jobs/{id}/resume` | For `interrupted` jobs |
| `GET` | `/api/jobs/{id}/events` | **SSE** |
| `GET` | `/api/events` | **SSE** — global firehose for the activity sidebar |

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
| `DELETE` | `/api/profiles/{id}/material` | Clear stored secrets |
| `POST` | `/api/profiles/{id}/mint` | Run the headless mint → `202 {job_id}` |
| `POST` | `/api/profiles/{id}/verify` | Fetch `verify_url` with the jar → `{ok, final_url, interstitial_detected, screenshot_url?}` |
| `GET` | `/api/profiles/{id}/coverage?site_id=N` | Which of a site's scope hosts the jar covers |
| `POST` | `/api/profiles/interactive` | Start an interactive browser session (M5) → `{session_id, vnc_url}` |
| `POST` | `/api/profiles/interactive/{sid}/save` | Persist the session as a profile |

---

## Feeds

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/sites/{id}/feeds` | |
| `POST` | `/api/sites/{id}/feeds` | `{url, kind?, interval_min?}` |
| `POST` | `/api/sites/{id}/feeds/discover` | Auto-find feeds for this site |
| `POST` | `/api/feeds/test` | `{url}` — parse without saving; returns format, entry count, samples, scope check |
| `PATCH` `DELETE` | `/api/feeds/{id}` | |
| `POST` | `/api/feeds/{id}/poll` | Poll now → `202` |
| `GET` | `/api/feeds/{id}/items` | `?status=pending\|captured\|failed` |
| `GET` | `/api/feeds/{id}/history` | Poll log |
| `POST` | `/api/feeds/{id}/capture-pending` | Capture all pending items |

---

## Engines & post-processors

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/engines` | Installed, with manifests and capabilities |
| `GET` | `/api/engines/{id}/schema` | JSON Schema for the config form |
| `POST` | `/api/engines/rescan` | Re-read `/config/engines` |
| `POST` | `/api/engines/{id}/validate` | Validate a config object without saving |
| `PATCH` | `/api/engines/{id}` | Enable/disable, set instance defaults |
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
| `POST` | `/api/maintenance/verify` | Checksum verification → `202` |
| `POST` | `/api/maintenance/rebuild-symlinks` | |
| `POST` | `/api/maintenance/rebuild-db` | Reconstruct DB rows from on-disk manifests |
| `POST` | `/api/maintenance/purge-trash` | |
| `GET` | `/api/audit` | Auth and admin events |
| `GET` | `/api/export/config` | Full config backup as JSON — **excludes secret material** |
| `POST` | `/api/import/config` | Restore |

`/api/health` is unauthenticated because Unraid's healthcheck and any uptime monitor need it. It therefore must leak nothing: no version-specific vulnerability hints beyond the version string, no paths, no site names, no counts.

`/api/version` is the split from that. `version` alone is useless for the question people actually ask — it reads `0.1.0` on every commit, so an image several milestones behind reports exactly what a current one does. `build` is the part that changes, and because it names a commit it also names that commit's known bugs, so it sits behind a session while `/health` keeps the bare version it always had. Resolution order is `CAIRN_BUILD`/`CAIRN_BUILT_AT` in the environment, then the `BUILD_INFO` file the image writes, then `git describe` in a source checkout, then the literal string `source`. Nothing is invented: a fabricated id would read as a real build nobody can find.

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
