# Cairn — Self-Hosted Website Archiver

> **Name is a placeholder.** A cairn is a stack of stones that marks a trail — rename freely (`docs/` uses `cairn` as the package/image/DB name throughout).

A single-user, web-UI-driven website archiving tool that runs as a Docker container on Unraid. It crawls whole domains to WARC, replays them in the browser, organizes them into folders and tags, and keeps them current from RSS/Atom feeds.

Built after evaluating ArchiveBox and finding its data model and organization a poor fit — see [archivebox-notes-and-alternatives.md](archivebox-notes-and-alternatives.md) for that evaluation, which drove most of the decisions here.

---

## What it does

| | |
|---|---|
| **Index first, then choose** | Point it at a seed URL. It reads sitemaps, feeds, and a shallow crawl, then shows you every domain the site touches — grouped, counted, role-guessed — and you tick the ones to include. |
| **Whole-domain capture** | One capture job covers `example.com/page1`, `/page2`, … as a single unit with one merged index, not hundreds of disconnected snapshots. |
| **Gets past Blogger interstitials** | Import a `cookies.txt` **or** a Tampermonkey userscript, per site, selected in the UI. Later: log in interactively in an embedded browser and save the session as a reusable profile. |
| **Browse the archive in the UI** | Full replay of the captured site through pywb, in an iframe, on an isolated origin. Click through the site as it was. |
| **Real organization** | Nested folders, tags, saved filters — and a generated symlink tree so the folder/tag structure exists on disk too, not just in a database. |
| **Stays current** | Associate RSS/Atom feeds (or sitemap diffs) with a site; new posts get captured into that site's folder automatically. |
| **Pluggable engines** | v1 ships wget→WARC. A documented addon contract lets you drop in browsertrix-crawler, SingleFile, yt-dlp, or your own without touching core. |

## Documentation

Read in order for a full picture; each is standalone if you're looking for one thing.

| Doc | What's in it |
|---|---|
| [00 — Decisions](docs/00-decisions.md) | Every significant technical choice, with rationale and what was rejected |
| [01 — Requirements](docs/01-requirements.md) | Your requirements traced to concrete design responses, plus scope boundaries |
| [02 — Architecture](docs/02-architecture.md) | Components, processes, data flow, tech stack |
| [03 — Data model & storage](docs/03-data-model-and-storage.md) | SQL schema, on-disk layout, naming, retention |
| [04 — Discovery & scoping](docs/04-discovery-and-scoping.md) | How the initial index works and how domain selection maps to crawl scope |
| [05 — Capture engines](docs/05-capture-engines.md) | The addon contract, engine protocol, and the full wget/WARC engine spec |
| [06 — Access profiles](docs/06-access-profiles.md) | Cookies, userscripts, the Blogger interstitial, credential storage |
| [07 — Replay](docs/07-replay.md) | pywb integration, indexing strategy, WACZ, replay security |
| [08 — Feeds & scheduling](docs/08-feeds-and-scheduling.md) | RSS/Atom watching, sitemap diffing, the scheduler |
| [09 — API](docs/09-api.md) | REST surface + SSE event stream |
| [10 — Deployment (Unraid)](docs/10-deployment-unraid.md) | Image build, compose, CA template, Unraid-specific gotchas |
| [11 — Security](docs/11-security.md) | Threat model and hardening for an internet-exposed instance |
| [12 — Roadmap](docs/12-roadmap.md) | M0–M8 milestones with exit criteria |
| [13 — Feature backlog](docs/13-feature-backlog.md) | Ideas borrowed from other tools, ranked by value/effort |
| [14 — Tooling landscape](docs/14-tooling-landscape.md) | Every relevant tool, what it's good at, whether to use it |

## Stack at a glance

- **Backend** — Python 3.12, FastAPI, SQLite (WAL), in-process async job runner
- **Frontend** — React + Vite + TypeScript, Tailwind + shadcn/ui, TanStack Query
- **Capture** — GNU wget → WARC (v1); addon engines beyond that
- **Replay** — pywb, sidecar process on a separate port
- **Packaging** — one Docker image, s6-overlay, `linuxserver`-style `PUID`/`PGID`

## Status

**M0 (foundation & auth) is complete** — see the [roadmap](docs/12-roadmap.md). You can run the container, create an account, and sign in. Capture, discovery and replay arrive in M1–M3.

## Running it

With Docker. Generate a master key first — compose reads `.env` and refuses to start without one, because a generated-per-restart key would silently orphan every stored credential:

```bash
cp .env.example .env && echo "CAIRN_SECRET_KEY=$(openssl rand -base64 48)" >> .env
```

```bash
docker compose up -d
```

Then open http://127.0.0.1:8080. Back up that key.

Or with plain `docker run` — the `-p` flags are not optional, without them the container starts, reports `healthy`, and is unreachable:

```bash
docker run -d --name cairn -p 8080:8080 -p 8081:8081 -v cairn-config:/config -v cairn-data:/data -e CAIRN_SECRET_KEY="$(openssl rand -base64 48)" --shm-size=2g ghcr.io/you/cairn:latest
```

For local development (Python 3.12+, Node 22+):

```bash
python -m venv .venv && .venv/Scripts/pip install -e ".[dev]"
```

```bash
cd frontend && npm install && npm run build
```

```bash
cp .env.example .env
```

```bash
.venv/Scripts/python -m uvicorn cairn.app:app --factory --port 8080
```

Then open http://localhost:8080 and create your account.

## First login

**There is no default username or password, and the setup page has no URL of its own.** The app decides between three states from `GET /api/health`: no account yet → setup screen; account exists but no session → sign-in; signed in → the app. Every path renders the right one, so `/`, `/setup` and `/anything` all behave the same.

Whatever you enter on the setup screen becomes the account. The endpoint behind it returns `409 Conflict` forever once any account exists, so it cannot be used to add a second one later.

### The page doesn't load at all

Check the container's state first — this failure has two very different shapes:

```bash
docker ps -a --filter name=cairn --format "{{.Names}} {{.Status}} {{.Ports}}"
```

**Read the `PORTS` column first — it is the single clearest tell:**

| `PORTS` shows | Meaning |
|---|---|
| `8080-8081/tcp` | **Not published.** The ports are only exposed inside Docker's network; nothing on your machine reaches them. The container still reports `healthy`, because the healthcheck runs *inside* it. Re-run with `-p 8080:8080 -p 8081:8081`, or in Docker Desktop expand **Optional settings** and fill in the host ports — it leaves them blank by default. |
| `0.0.0.0:8080->8080/tcp` | Published correctly. If it still fails, look at `STATUS` below. |

| `STATUS` shows | Meaning |
|---|---|
| `Exited (78)` | Configuration error the app cannot fix itself. The logs print a banner naming the problem and the fix — most often a `CAIRN_SECRET_KEY` that doesn't match the one the database was created with. |
| `Up (healthy)` with ports published | The app is serving. Check the URL and any reverse proxy in front of it. |
| `Exited (0)` / restarting | Check the logs for the startup banner. |

Note that `docker run` without `--name` assigns a random one like `optimistic_brahmagupta`, and the `docker exec cairn …` commands below need that actual name. Pass `--name cairn` to keep them working.

If you set `CAIRN_SECRET_KEY` in Docker Desktop, note it takes **Name** and **Value** as two fields — the name is `CAIRN_SECRET_KEY` and the value is the key alone, not `CAIRN_SECRET_KEY=…`.

Changing the key is only fatal once something has actually been sealed under the old one (2FA secrets, recovery codes, cookie jars). Before that, the new key is simply adopted and logged. To see which key is in use:

```bash
docker exec cairn cairn key-info
```

If you lost the old key and accept losing what it sealed:

```bash
docker exec cairn cairn reset-key --force
```

### Seeing Sign In instead of the setup screen

That means an account already exists — the app never skips setup on a genuinely empty instance. Almost always the `/config` volume carries over from a previous run. Confirm it:

```bash
docker exec cairn cairn users
```

`No account exists yet` means you are talking to a different process than you think — check nothing else is bound to that port. Otherwise use the recovery commands below, or point the container at an empty config directory to start fresh.

Requirements: username 3–64 characters (letters, digits, `.`, `-`, `_`); password at least 12 characters, not a well-known one. Turn on two-factor authentication right after, in Settings.

### If you get locked out

All of these need shell access to the container, which is the recovery boundary — there is no email reset and no forgot-password link.

Check the state of the account:

```bash
docker exec cairn cairn users
```

Reset the password (also clears any lockout, and signs out every session):

```bash
docker exec -it cairn cairn reset-password admin
```

If your console has no TTY (Unraid's browser terminal, or `docker exec` without `-it`), pipe it instead:

```bash
docker exec -i cairn sh -c 'echo "your-new-passphrase" | cairn reset-password admin --stdin'
```

Locked out by failed attempts but you *do* know the password — just clear the lockout:

```bash
docker exec cairn cairn unlock admin
```

Lost your authenticator and your recovery codes:

```bash
docker exec cairn cairn disable-totp admin
```

Starting completely over wipes the archives too, so prefer the commands above. If you truly want a clean slate, stop the container and delete `cairn.db` from your config volume.

The checks CI runs:

```bash
.venv/Scripts/python -m pytest -q
```

```bash
.venv/Scripts/ruff check . && .venv/Scripts/ruff format --check . && .venv/Scripts/mypy
```

## Repository layout

```
backend/cairn/      FastAPI app, services, models, migrations, CLI
frontend/           React + Vite SPA (builds into backend/cairn/static)
docker/rootfs/      s6-overlay service definitions
unraid/             Community Applications template
tests/              pytest suite
docs/               design documentation — read 00-decisions.md first
```

## A note on responsible use

This is a personal archiving tool. Default behavior respects `robots.txt`, rate-limits requests, and sends an identifying user agent. The UI exposes overrides (Blogger's `robots.txt` blocks `/search`, which is where label pages live) — use them on sites you own or have permission to archive, and keep the concurrency and rate limits polite regardless.
