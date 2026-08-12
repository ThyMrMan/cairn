<!--
  Badges go here once this repository has a URL. The CI workflow is
  .github/workflows/ci.yml; the badge is
  ![CI](https://github.com/ThyMrMan/cairn/actions/workflows/ci.yml/badge.svg)
-->

# Cairn

**A self-hosted website archiver with a web UI. Crawls whole domains to WARC,
replays them in your browser, and keeps them current from RSS.**

Point it at a blog. It works out which domains that blog actually pulls from,
shows you the list, and lets you tick the ones worth keeping. Then it crawls
them to WARC, indexes them, and puts the site back on screen — pages, images,
CSS, pagination — as it was on the day it was captured. Add a feed and new
posts arrive on their own.

Built for a NAS. One Docker container, one SQLite database, and an archive tree
on disk you can browse over SMB without the application running at all.

> [!NOTE]
> **Single-user by design.** One account, and no roles, sharing or per-user
> quotas — this is one person archiving their own reading, and the design says
> so everywhere. See [scope boundaries](docs/01-requirements.md).

---

## Highlights

| | |
|---|---|
| **Index first, then choose** | Reads sitemaps, feeds and a shallow crawl, then shows every domain the site touches — grouped, counted, role-guessed — with two checkboxes each: crawl its pages, fetch its files. On a Blogger blog the answer arrives already correct. |
| **Whole-domain capture** | One capture job covers a site as a single unit with one merged index, not hundreds of disconnected page snapshots. |
| **Gets past content warnings** | Import a `cookies.txt`, run a Tampermonkey userscript, or sign in yourself in an embedded Chromium and save the session. Per site, selected in the UI. |
| **Replay in the browser** | Full pywb replay in an iframe on an isolated origin. Click through the archived site; switch between captures of the same page. |
| **Full-text search** | One query across every archived page, with the boilerplate stripped, opening the version that matched. |
| **Real organisation** | Nested folders and tags that exist as actual directories and symlinks under `/data`, not just rows in a database. |
| **Stays current** | Watch a feed, a sitemap, or a page's text. New posts are captured incrementally at a fraction of a full crawl. |
| **Tells you when it breaks** | Notifications, a digest of what quietly *stopped* happening, live-site health checks, and a weekly checksum pass over every archived byte. |
| **Pluggable engines** | Ships wget→WARC and browsertrix-crawler. A documented NDJSON contract lets you add your own in any language. |
| **Portable output** | Standard WARC, a CDXJ index, and one-file `.wacz` export that [ReplayWeb.page](https://replayweb.page/) opens with no server. |

A longer walk through every feature, and what each one deliberately does not
do, is in the **[feature tour](docs/15-feature-tour.md)**.

---

## Quick start

### Docker Compose

Generate a master key first. Compose reads `.env` and refuses to start without
one, because a key regenerated per restart would silently orphan every stored
credential:

```bash
cp .env.example .env && echo "CAIRN_SECRET_KEY=$(openssl rand -base64 48)" >> .env
```

```bash
docker compose up -d
```

Open <http://127.0.0.1:8080> and create your account. **Back up that key** —
losing it makes stored cookie jars unrecoverable.

### Plain Docker

The `-p` flags are not optional. Without them the container starts, reports
`healthy`, and is unreachable — the healthcheck runs inside it.

```bash
docker run -d --name cairn -p 8080:8080 -p 8081:8081 -v cairn-config:/config -v cairn-data:/data -e CAIRN_SECRET_KEY="$(openssl rand -base64 48)" --shm-size=2g ghcr.io/thymrman/cairn:latest
```

### Unraid

A Community Applications template is in [`unraid/`](unraid/). Put `/config` on
the cache pool and `/data` on the array — see
[10 — Deployment](docs/10-deployment-unraid.md).

### First login

**There is no default username or password**, and the setup page has no URL of
its own. The app picks between three states from `GET /api/health`: no account
yet → setup; account but no session → sign-in; signed in → the app. Whatever
you enter on the setup screen becomes the account, and that endpoint returns
`409 Conflict` forever afterwards, so it cannot be used to add a second one.

Username 3–64 characters (letters, digits, `.`, `-`, `_`); password at least 12
characters and not a well-known one. Turn on two-factor authentication straight
afterwards, in Settings.

Something wrong? → **[16 — Troubleshooting](docs/16-troubleshooting.md)**.

---

## Configuration

Everything a person can change while it runs lives in **Settings**. These are
the environment variables, which need a restart.

| Variable | Default | What it does |
|---|---|---|
| `CAIRN_SECRET_KEY` | — | **Required.** Seals cookie jars, 2FA secrets and recovery codes at rest. Generate with `openssl rand -base64 48`. Back it up. |
| `CAIRN_CONFIG_DIR` | `/config` | Database, settings, engines, backups. Small and hot — put it on an SSD. |
| `CAIRN_DATA_DIR` | `/data` | The archive tree. Large and cold — an array is fine. |
| `CAIRN_PORT` | `8080` | The app. |
| `CAIRN_REPLAY_PORT` | `8081` | pywb, on its own origin. |
| `CAIRN_APP_PUBLIC_URL` | — | Set when behind a reverse proxy. |
| `CAIRN_REPLAY_PUBLIC_URL` | — | Likewise — and it **must differ from the app in hostname**, not merely in port. Ports do not isolate cookies. |
| `CAIRN_MAX_CONCURRENT_JOBS` | `2` | Parallel captures. Per-host serialisation applies regardless. |
| `CAIRN_TRUSTED_PROXY` | — | CIDR allowed to set `X-Forwarded-For`. Without it the header is ignored, which is what keeps the login rate limiter honest. |
| `CAIRN_LOG_LEVEL` / `CAIRN_LOG_JSON` | `INFO` / `true` | Structured logs to stdout, with secrets redacted. |
| `PUID` / `PGID` / `UMASK` | `1000`/`1000`/`022` | `linuxserver`-style. Files on the share are written as this user. |

`--shm-size=2g` is not optional if you want the browser-backed features:
Chromium crashes on Docker's default 64 MB `/dev/shm`.

---

## How it works

```
seed URL ──► discovery ──► domain picker ──► scope ──► capture engine ──► WARC
                (robots,        (you tick)     (wget      (wget or         │
                 sitemaps,                      args)      browsertrix)    │
                 feeds, sample)                                            ▼
                                                              post-processing
   replay ◄── CDXJ index ◄────────────────────────────────  checksum, stats,
   (pywb)                                                    index, text,
      │                                                      thumbnail, media
      └──► search · reader view · diffs · WACZ export · integrity checks
```

- **Discovery** decides *what* gets captured. Getting it wrong means
  recapturing everything later, which is why it comes before anything else.
- **The engine** is a subprocess that speaks NDJSON on stdout. Cairn never
  imports engine code.
- **The index** spans every capture a site has ever had, which is where
  replay's time dimension comes from — not the directory a WARC sits in.
- **Everything derived** — index, extracted text, search, thumbnails — is
  regenerable from the WARCs, and is never the only copy of anything.

Full detail in [02 — Architecture](docs/02-architecture.md).

---

## Documentation

Design documents, written before the code and corrected from it. Every one
carries what the implementation taught us it got wrong.

| Doc | What is in it |
|---|---|
| [00 — Decisions](docs/00-decisions.md) | Every significant technical choice, with rationale and what was rejected |
| [01 — Requirements](docs/01-requirements.md) | Requirements traced to concrete design responses, plus scope boundaries |
| [02 — Architecture](docs/02-architecture.md) | Components, processes, data flow, tech stack |
| [03 — Data model & storage](docs/03-data-model-and-storage.md) | SQL schema, on-disk layout, naming, retention |
| [04 — Discovery & scoping](docs/04-discovery-and-scoping.md) | How the initial index works and how domain selection becomes crawl scope |
| [05 — Capture engines](docs/05-capture-engines.md) | The addon contract, the engine protocol, the wget/WARC engine, post-processors |
| [06 — Access profiles](docs/06-access-profiles.md) | Cookies, userscripts, interactive login, credential storage |
| [07 — Replay](docs/07-replay.md) | pywb integration, indexing strategy, WACZ, replay security |
| [08 — Feeds & scheduling](docs/08-feeds-and-scheduling.md) | RSS/Atom watching, sitemap diffing, the scheduler, notifications |
| [09 — API](docs/09-api.md) | REST surface and the SSE event stream |
| [10 — Deployment (Unraid)](docs/10-deployment-unraid.md) | Image build, compose, CA template, Unraid-specific gotchas |
| [11 — Security](docs/11-security.md) | Threat model and hardening for an internet-exposed instance |
| [12 — Roadmap](docs/12-roadmap.md) | Milestones with exit criteria, and what each one got wrong |
| [13 — Feature backlog](docs/13-feature-backlog.md) | Ideas from other tools, ranked, plus the anti-features |
| [14 — Tooling landscape](docs/14-tooling-landscape.md) | Every relevant tool, what it is good at, whether to use it |
| [15 — Feature tour](docs/15-feature-tour.md) | What using it is actually like, feature by feature |
| [16 — Troubleshooting](docs/16-troubleshooting.md) | Recovery, lockouts, broken symlink trees, replay 404s |

Cairn exists because [ArchiveBox](https://archivebox.io/) was evaluated first
and its data model and organisation were a poor fit for whole-domain archiving.
That evaluation is in
[archivebox-notes-and-alternatives.md](archivebox-notes-and-alternatives.md)
and it drove most of the decisions above.

---

## Development

Python 3.12+ and Node 22+.

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
```

```bash
cd frontend && npm install && npm run build
```

```bash
cp .env.example .env && .venv/bin/python -m uvicorn cairn.app:app --factory --port 8080
```

On Windows the venv scripts live in `.venv/Scripts/` rather than `.venv/bin/`.

What CI runs:

```bash
pytest -q
```

```bash
ruff check . && ruff format --check . && mypy
```

The end-to-end tests need GNU wget, pywb and Chromium, and are skipped when any
is missing — so running them on a bare development machine silently tests less
than it appears to. They are skipped on Windows outright: Git for Windows ships
a mingw32 wget whose WARC temp files hit the 260-character path limit inside
pytest's temp directories. Run them in the container instead:

```bash
docker build -t cairn:latest . && docker build -t cairn:dev -f docker/Dockerfile.dev .
```

```bash
docker run --rm -v "$PWD:/app" -w /app cairn:dev pytest -q
```

Use `cairn:dev` for this and not `cairn:latest`. The runtime image's entrypoint
is s6, so anything run through it starts the app and pywb alongside the tests —
which is not a neutral environment, and its failure mode is a test passing
against the wrong server rather than erroring.

Rebuild `cairn:dev` whenever you rebuild `cairn:latest`. A stale one keeps
whatever tooling the runtime image had when it was built, and a suite that
skips because a tool is missing looks exactly like one you opted out of. `-rs`
prints the reason for every skip.

Container-engine tests additionally need the Docker socket **and**
`CAIRN_TEST_CONTAINERS=1` — a deliberate opt-in, because they pull most of a
gigabyte.

### Repository layout

```
backend/cairn/      FastAPI app, services, models, migrations, CLI
  engines/          the addon contract, and the built-in wget-warc engine
  services/         scope, storage, jobs, profiles, post-processing
frontend/           React + Vite SPA (builds into backend/cairn/static)
docker/rootfs/      s6-overlay service definitions
unraid/             Community Applications template
examples/           engine-template — copy this to write your own
tests/              pytest suite
docs/               design documentation — read 00-decisions.md first
```

### Stack

Python 3.12 · FastAPI · SQLite (WAL) · React + Vite + TypeScript · Tailwind ·
TanStack Query · GNU wget → WARC · pywb · Playwright/Chromium · s6-overlay.

---

## Project status

**Working end to end and in use.** M0–M8 are complete, along with everything in
the backlog worth building: browser-based discovery, multi-seed sites, the
digest, reader view, site health, bulk import, the bookmarklet, annotations,
backup verification and site thumbnails. See the
[roadmap](docs/12-roadmap.md) for the milestone-by-milestone record.

742 tests pass in the container with 3 skipped — the container-engine suites,
which need the Docker socket and a deliberate opt-in. Lint, format and strict
type checks are clean.

The image carries Chromium for the userscript, interactive-login, discovery and
thumbnail paths, which puts it at roughly **1.7 GB**. Everything else works
without it.

Not built, deliberately, each with the reasoning recorded: public share links,
storage tiering, a third capture engine, and multi-user.

> **The name is a placeholder.** A cairn is a stack of stones that marks a
> trail. Rename freely — `cairn` is used consistently as the package, image and
> database name, so it is a global find-and-replace.

---

## Security

Three properties shape the whole design, and they are worth knowing before you
expose this to anything:

- **Replay executes untrusted JavaScript.** Every archived page contains code
  that runs in your browser when you view it. Replay is therefore served from a
  separate origin, and behind a proxy that must be a separate *hostname* —
  ports do not isolate cookies.
- **The app fetches arbitrary URLs by design.** SSRF is the feature. Media URLs
  extracted from archived HTML — the one genuinely attacker-controlled target —
  are checked against private ranges after DNS resolution.
- **It stores session cookies.** They are sealed at rest with
  `CAIRN_SECRET_KEY`, never returned by the API, and never logged.

The full threat model is [11 — Security](docs/11-security.md). The short
version: **do not expose it directly if you can avoid it.** Tailscale or a
Cloudflare Tunnel removes the internet-facing surface entirely, which is better
advice than any amount of in-app hardening.

Found a vulnerability? [SECURITY.md](SECURITY.md) says how to report it and
what is in scope.

---

## Responsible use

This is a personal archiving tool. Default behaviour respects `robots.txt`,
rate-limits requests, serialises per host, and sends an identifying user agent.

The UI exposes overrides — Blogger's `robots.txt` blocks `/search`, which is
where label pages live — and they are there for sites you own or have
permission to archive. Keep the concurrency and rate limits polite regardless;
two simultaneous crawls of one blog is what gets an archiver blocked, whoever
started them.

Cairn does not circumvent paywalls or CAPTCHAs and will not grow the ability
to. Access profiles let *you* supply credentials you already have.

---

## License

MIT. See [LICENSE](LICENSE).
