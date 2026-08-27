# 10 — Deployment on Unraid

Covers R3. Target: one container, installable from Community Applications, no external dependencies.

---

## Container layout

One image, s6-overlay supervising two services ([D14](00-decisions.md#d14--single-docker-image-multiple-processes)).

| | |
|---|---|
| **Ports** | `8080` app (UI + API), `8081` replay (pywb) |
| **Volumes** | `/config` → cache pool, `/data` → array |
| **User** | Drops to `PUID`:`PGID` (Unraid convention `99`:`100` = `nobody`:`users`) |
| **Healthcheck** | `curl -f localhost:8080/api/health` |
| **Base** | `python:3.12-slim` + wget (PCRE-enabled), pywb, s6-overlay |

### Dockerfile sketch

```dockerfile
FROM python:3.12-slim

ARG S6_OVERLAY_VERSION=3.2.0.2
RUN apt-get update && apt-get install -y --no-install-recommends \
      wget ca-certificates tzdata gosu xz-utils curl \
 && rm -rf /var/lib/apt/lists/*

# Scope regexes depend on PCRE lookahead — fail the build loudly rather than
# at crawl time. This compiles a real pattern rather than grepping the version
# banner: Debian's wget links PCRE2 and honours lookahead, but reports neither
# "+pcre" nor "-pcre" (that flag only described PCRE1), so a banner check
# rejects a perfectly good build. See docs/04.
RUN set -eux; \
    probe="$(wget --regex-type=pcre \
                  --reject-regex='^https?://example\.com/(?!.*\.jpg$).*$' \
                  --spider --tries=1 --timeout=1 http://127.0.0.1:1/ 2>&1 || true)"; \
    case "$probe" in \
      *"Invalid regular expression"*|*"Invalid value"*) \
        echo "FATAL: this wget cannot compile a PCRE lookahead"; exit 1 ;; \
    esac

ADD https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}/s6-overlay-noarch.tar.xz /tmp/
RUN tar -C / -Jxpf /tmp/s6-overlay-noarch.tar.xz

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt      # fastapi, sqlalchemy, alembic,
                                                        # pywb, warcio, cdxj-indexer,
                                                        # feedparser, tldextract, selectolax,
                                                        # apscheduler, argon2-cffi, cryptography

COPY --from=frontend-build /app/dist /opt/cairn/static
COPY backend/ /opt/cairn/
COPY docker/rootfs/ /

ENV CAIRN_DATA_DIR=/data \
    CAIRN_CONFIG_DIR=/config \
    CAIRN_PORT=8080 \
    CAIRN_REPLAY_PORT=8081 \
    PUID=99 PGID=100 UMASK=022

VOLUME ["/config", "/data"]
EXPOSE 8080 8081
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s \
  CMD curl -fsS http://localhost:8080/api/health || exit 1
ENTRYPOINT ["/init"]
```

That functional probe is worth keeping. Without PCRE, `--reject-regex` falls back to POSIX ERE with no lookahead, and the asset-host scope regexes from [04](04-discovery-and-scoping.md#translation-to-wget) silently stop working — which surfaces as a crawl that either wanders onto image CDNs or drops all images, hours later.

It must stay a *functional* check. The obvious `wget --version | grep +pcre` is wrong and was the first thing this project got bitten by: Debian's wget 1.25.0 compiles lookahead correctly but prints no pcre flag at all, so the banner check fails a working build.

### s6 services

```
/etc/s6-overlay/s6-rc.d/
  init-perms/          oneshot: chown /config /data to PUID:PGID, apply UMASK
  init-migrate/        oneshot: alembic upgrade head (after init-perms)
  init-tmp-sweep/      oneshot: wipe /data/tmp (leftover plaintext cookie jars)
  init-pywb-config/    oneshot: regenerate /config/pywb/config.yaml from the DB
  cairn/               longrun: uvicorn (depends on init-migrate)
  pywb/                longrun: wayback (depends on init-pywb-config)
```

`init-perms` recursively chowning a multi-terabyte `/data` on every boot is slow and pointless. Chown `/config` fully; for `/data` chown only the top level and any path whose owner is wrong, or gate it behind a `CAIRN_FIX_PERMS=1` flag that users set once.

---

## Unraid template

`cairn.xml` for Community Applications:

```xml
<?xml version="1.0"?>
<Container version="2">
  <Name>Cairn</Name>
  <Repository>ghcr.io/thymrman/cairn:latest</Repository>
  <Registry>https://ghcr.io/thymrman/cairn</Registry>
  <Network>bridge</Network>
  <Shell>bash</Shell>
  <Privileged>false</Privileged>
  <Support>https://github.com/ThyMrMan/cairn/discussions</Support>
  <Project>https://github.com/ThyMrMan/cairn</Project>
  <Overview>
    Self-hosted website archiver. Crawls whole sites to WARC, replays them in your
    browser, organizes them into folders and tags, and keeps them current from RSS feeds.
  </Overview>
  <Category>Productivity: Tools:Utilities</Category>
  <WebUI>http://[IP]:[PORT:8080]/</WebUI>
  <Icon>https://raw.githubusercontent.com/ThyMrMan/cairn/main/docs/icon.png</Icon>
  <ExtraParams>--shm-size=2g</ExtraParams>

  <Config Name="WebUI Port" Target="8080" Default="8080" Mode="tcp"
          Type="Port" Required="true">Web interface.</Config>
  <Config Name="Replay Port" Target="8081" Default="8081" Mode="tcp"
          Type="Port" Required="true">Archive replay (pywb). Must be a separate origin from the WebUI.</Config>

  <Config Name="Config" Target="/config" Default="/mnt/user/appdata/cairn"
          Type="Path" Mode="rw" Required="true">
    Database and settings. MUST be on a cache pool / SSD — not the array.
  </Config>
  <Config Name="Archives" Target="/data" Default="/mnt/user/archives/cairn"
          Type="Path" Mode="rw" Required="true">
    WARC storage. Array is fine; this is where the bulk lives.
  </Config>

  <Config Name="CAIRN_SECRET_KEY" Target="CAIRN_SECRET_KEY" Default=""
          Type="Variable" Mask="true" Required="true">
    Encrypts stored cookies and signs sessions. Generate with:
    openssl rand -base64 48    — SAVE THIS. Losing it makes stored credentials unrecoverable.
  </Config>
  <Config Name="CAIRN_REPLAY_PUBLIC_URL" Target="CAIRN_REPLAY_PUBLIC_URL" Default=""
          Type="Variable" Required="false">
    External URL of the replay origin, e.g. https://replay.example.com.
    Required if you reverse-proxy this. Must differ in HOSTNAME from the WebUI, not just port.
  </Config>
  <Config Name="PUID" Target="PUID" Default="99" Type="Variable" Required="true"/>
  <Config Name="PGID" Target="PGID" Default="100" Type="Variable" Required="true"/>
  <Config Name="UMASK" Target="UMASK" Default="022" Type="Variable" Required="false"/>
  <Config Name="TZ" Target="TZ" Default="America/New_York" Type="Variable" Required="true"/>
  <Config Name="CAIRN_MAX_CONCURRENT_JOBS" Target="CAIRN_MAX_CONCURRENT_JOBS"
          Default="2" Type="Variable" Required="false"/>
  <Config Name="CAIRN_REPLAY_UNCOVER_OVERLAYS" Target="CAIRN_REPLAY_UNCOVER_OVERLAYS"
          Default="true" Type="Variable" Required="false">
    Show pages that a site archived in full and then drew a content warning over.
    Set false to replay them exactly as stored, warning and all. Either way the
    WARC is untouched — see docs/07.
  </Config>
</Container>
```

`--shm-size=2g` was set from the very first template, before anything in the image needed it. Chromium crashes with cryptic renderer errors on Docker's default 64 MB `/dev/shm`, and changing a template variable after installation is a support-thread generator — so the room was made before the thing that needs it arrived. It is now used by userscript and interactive profiles, browser-based discovery, and site thumbnails; everything else works without it.

---

## The SQLite-on-FUSE footgun

**The most important Unraid-specific point in this document.**

Unraid's `/mnt/user` is a FUSE overlay (shfs) across the array and cache pools. SQLite's locking is not reliable through it, and if the share is array-backed with mover active, the DB file can be relocated *while open*. The standard failure is `database is malformed` after weeks of apparently fine operation.

**Requirements:**
1. `/config` must be on a **cache pool**, on a share set to **cache-only** (not "prefer", not "yes"). Mover must never touch it.
2. `/data` on the array is fine — WARC writes are sequential, large, and don't use file locking.
3. Detect this at startup: `statfs` the `/config` mount, and if the filesystem type looks like FUSE/shfs, log a loud warning with the fix. Don't refuse to start — some people run this on plain Docker where the check is meaningless — but make it impossible to miss.

Also keep `index/*.cdxj` on fast storage if you can. pywb binary-searches these on every replay request; on spinning disks it's noticeably sluggish. A per-site option to keep indexes under `/config/indexes/` while the WARCs live on the array is a reasonable M8 refinement.

---

## Environment variables

Every setting that needs a restart, in full. The README carries the dozen most
people set; this is all of them, and each maps to a field on
`cairn.config.Settings` with the `CAIRN_` prefix. Everything a person can
change *while it runs* is in **Settings** instead and lives in the database.

| Variable | Default | What it does |
|---|---|---|
| `CAIRN_SECRET_KEY` | — | **Required.** Seals cookie jars, 2FA secrets and recovery codes. Back it up: losing it makes stored credentials unrecoverable |
| `CAIRN_CONFIG_DIR` | `/config` | Database, settings, engines, backups. Small and hot |
| `CAIRN_DATA_DIR` | `/data` | The archive tree. Large and cold |
| `CAIRN_HOST` | `0.0.0.0` | Bind address inside the container. Exposure is the port mapping's business, not this |
| `CAIRN_PORT` | `8080` | The app |
| `CAIRN_REPLAY_PORT` | `8081` | The port pywb **binds** inside the container |
| `CAIRN_REPLAY_PUBLIC_PORT` | `0` | The port a **browser** should use, when it differs. `0` means "same as `CAIRN_REPLAY_PORT`". Needed whenever the replay port is published on a different host port — nothing inside the container can discover the published number |
| `CAIRN_APP_PUBLIC_URL` | — | Set behind a reverse proxy |
| `CAIRN_REPLAY_PUBLIC_URL` | — | Likewise, and it **must differ from the app in hostname**, not merely in port |
| `CAIRN_REPLAY_UNCOVER_OVERLAYS` | `true` | Whether replay lifts a content warning a site drew over a page it had already sent in full. Off makes replay byte-faithful ([07](07-replay.md#uncovering-a-page-the-site-drew-a-warning-over)) |
| `CAIRN_MAX_CONCURRENT_JOBS` | `2` | Parallel captures. Per-host serialisation applies regardless |
| `CAIRN_TRUSTED_PROXY` | — | CIDR allowed to set `X-Forwarded-For`. Without it the header is ignored, which is what keeps the login rate limiter honest |
| `CAIRN_AUTH_HEADER_MODE` | `false` | Trust an upstream proxy's authentication header instead of the login form. Only safe when the app is unreachable except through that proxy |
| `CAIRN_AUTH_HEADER_NAME` | `Remote-User` | Which header, when the above is on |
| `CAIRN_SESSION_IDLE_DAYS` | `7` | A session expires this long after its last use |
| `CAIRN_SESSION_ABSOLUTE_DAYS` | `30` | And this long after it was created, used or not |
| `CAIRN_LOGIN_MAX_ATTEMPTS` | `5` | Failures before a lockout |
| `CAIRN_LOGIN_WINDOW_SECONDS` | `900` | The window they have to fall inside |
| `CAIRN_LOGIN_LOCKOUT_SECONDS` | `3600` | How long the lockout lasts |
| `CAIRN_PASSWORD_MIN_LENGTH` | `12` | Enforced at setup and at every change |
| `CAIRN_COOKIE_SECURE` | inferred | Force the `Secure` flag on the session cookie. Unset, it follows the scheme of the public URL — set it explicitly if a proxy terminates TLS and the app does not know |
| `CAIRN_COOKIE_NAME` | `cairn_session` | Rename to run two instances on one hostname |
| `CAIRN_LOG_LEVEL` | `INFO` | |
| `CAIRN_LOG_JSON` | `true` | Structured logs to stdout, with secrets redacted |
| `CAIRN_DEV_MODE` | `false` | Serves the OpenAPI browser at `/api/docs`. Off in the shipped image |

`PUID`, `PGID` and `UMASK` (`1000`/`1000`/`022`) are `linuxserver`-style and
handled by the entrypoint rather than by `Settings` — they decide the ownership
of files written to the share.

---

## Reverse proxy

Both origins must be proxied, and **replay must get its own hostname**. Ports do not isolate cookies ([07](07-replay.md#the-cookie-scope-trap)) — same host on a different port shares the session cookie with archived JavaScript.

### SWAG / nginx

```nginx
# App
server {
    listen 443 ssl http2;
    server_name archive.example.com;

    location / {
        proxy_pass http://cairn:8080;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # SSE — these three lines matter; without them live logs stall
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 3600s;

        client_max_body_size 32m;   # cookie jars and userscripts
    }
}

# Replay — SEPARATE HOSTNAME, not just a separate port
server {
    listen 443 ssl http2;
    server_name replay.example.com;

    location / {
        proxy_pass http://cairn:8081;
        proxy_set_header Host              $host;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

Set `CAIRN_REPLAY_PUBLIC_URL=https://replay.example.com` and `CAIRN_TRUSTED_PROXY` to the proxy's address so `X-Forwarded-For` is honored for rate limiting and the audit log — and *only* then, since trusting those headers from arbitrary sources lets anyone spoof their IP past the login rate limiter.

### Cloudflare Tunnel

Works well and avoids opening ports at all. Two hostnames, two ingress rules. Note that Cloudflare's default proxy timeout will cut idle SSE connections around 100 s — the 15 s heartbeat in [09](09-api.md#sse) handles this.

### Tailscale

The simplest secure answer for a personal tool: put the Unraid host on a tailnet, don't expose anything publicly, reach it at `http://unraid:8080`. Auth still matters (a compromised device on the tailnet is still a threat), but the internet-facing attack surface goes to zero.

---

## docker-compose (non-Unraid)

```yaml
services:
  cairn:
    image: ghcr.io/thymrman/cairn:latest
    container_name: cairn
    restart: unless-stopped
    ports:
      - "8080:8080"
      - "8081:8081"
    volumes:
      - ./config:/config
      - /srv/archives:/data
    environment:
      - CAIRN_SECRET_KEY=${CAIRN_SECRET_KEY:?set this}
      - CAIRN_REPLAY_PUBLIC_URL=https://replay.example.com
      - PUID=1000
      - PGID=1000
      - TZ=America/New_York
    shm_size: 2gb
    healthcheck:
      test: ["CMD", "curl", "-fsS", "http://localhost:8080/api/health"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 30s
```

---

## First run

1. Container starts, migrations run, `/config/cairn.db` created.
2. No user exists → the UI serves a **setup** page (and only that page) to create the account. First-run setup is reachable without auth by necessity; it must become permanently unreachable the moment a user exists.
3. Optionally enable TOTP immediately.
4. Add the first site.

If `CAIRN_SECRET_KEY` is unset, generate one, write it to `/config/secret.key` (mode `600`), and log it prominently with instructions to move it into the env var. If sealed secrets exist and the key is missing or different, **refuse to start** with a clear message — silently starting with a fresh key would leave every stored cookie jar undecryptable with no indication of why.

---

## Backup

Two very different classes of data:

| What | Size | Strategy |
|---|---|---|
| `/config` | Megabytes | Unraid CA Backup plugin, nightly. This is the irreplaceable metadata |
| `/data/archives` | Terabytes | `restic` or `rclone` to offsite/cloud, weekly or monthly |

Back up `/config` with the container **stopped**, or use SQLite's online backup API rather than copying the file live. Copying a WAL-mode database mid-write produces a file that restores as corrupt.

**A `/data` backup is self-sufficient.** `site.yaml` + `manifest.json` + the WARCs are enough to rebuild the database (`POST /api/maintenance/rebuild-db`). Losing `/config` costs tags, schedules, and job history — not archives. That property is worth verifying with an actual restore test rather than assuming.

---

## Resource expectations

| State | RAM | CPU | Disk I/O |
|---|---|---|---|
| Idle | 150–250 MB | ~0% | negligible |
| Discovery | +100 MB | 1 core, bursty | light |
| wget capture (2k pages) | +200–600 MB | <1 core | sustained write, rate-limited |
| wget capture (100k pages) | +2–4 GB | <1 core | as above |
| Cookie mint (Chromium) | +400–800 MB | 1–2 cores for ~30 s | light |
| pywb replay | 100–200 MB | spiky per request | random reads on the index |

wget's memory grows with the visited-URL set and the WARC dedup index — that's the 100k-page row. Set a container memory limit somewhat above your largest expected crawl so a runaway job gets OOM-killed rather than taking the NAS down with it.

---

## Upgrades

- Migrations run automatically on start (`alembic upgrade head`), after a timestamped copy of the DB is taken into `/config/backups/`.
- Engine manifests are re-read on start; an addon whose schema no longer validates is disabled with an error rather than crashing the app.
- pywb config is regenerated on start, so upgrades that change its format self-heal.
- WARC files are never touched by an upgrade. Archives are forward-compatible by construction.
- Pin the image tag (`:1.2.0`) rather than `:latest` if the instance matters — Unraid auto-update on `:latest` combined with an unattended migration is how weekend outages happen.

---

## Publishing the image

`ghcr.io/thymrman/cairn`, pushed by CI and by nothing else. Three tags:

| Tag | When |
|---|---|
| `<commit sha>` | every push to `main` and every `v*` tag |
| `latest` | pushes to `main` |
| `1.2.0` | the tag `v1.2.0` |

**The published image is the one that was tested, not a rebuild of it.** The workflow builds once, starts that container, waits for `/api/health`, scans it, and only then re-tags and pushes the same artifact. Building a second time for the push would be a different artifact from the one the evidence is about, however identical the inputs look — and every so often it genuinely is different.

**No personal access token exists for this.** The runner authenticates with the `GITHUB_TOKEN` that GitHub mints per run, which is why the job declares `packages: write`. Nothing needs storing in a repository secret, and nothing needs a `docker login` on anybody's machine.

Publishing is gated on `github.repository` as well as the branch, so a fork's CI builds and tests and publishes nothing — a pull request from a fork must not be able to push an image to the upstream namespace.

The commit tag is what makes a running container traceable: it matches `CAIRN_BUILD`, which is baked into `BUILD_INFO` at build time and shown in **Settings → About**. A version alone cannot answer "which build is this" ([09](09-api.md#system)).

**One manual step, once.** A new GHCR package is private, and the first push creates it. Making it pullable without a login is done in the package's own settings on GitHub — *Package settings → Change visibility → Public* — and it cannot be done from the workflow. Until then `docker pull` asks for credentials, which looks exactly like a broken image reference.

---

## Backups, and checking them

The archive tree is the thing worth backing up: WARCs are immutable ([D2](00-decisions.md#d2--index-across-warcs-never-merge-or-concatenate-them)), so a copy is append-only and every incremental run is cheap. Use whatever you already run — `rsync -a`, restic, rclone. Cairn does not sync, and deliberately: those tools have resumption, bandwidth limits, encryption and deduplication that a bespoke implementation would spend years catching up to.

What Cairn has that they do not is the checksum taken when each file was written. So mount the copy read-only:

```
-v /mnt/backup/cairn:/backup:ro
```

and in **Settings → Check a backup copy**, type `/backup`. Two questions, in increasing cost:

- **Is it complete?** A directory listing, instant, and the one that catches the failure a sync reports success for — a directory that was skipped.
- **Are the bytes still the bytes?** The full integrity pass against the copy, using this instance's recorded checksums. It reads every byte of the backup, so it is a job.

A path inside `/data` is refused: checking the archive against itself would pass and mean nothing. Site directories in the copy that this instance has never heard of are reported and not treated as an error — an old backup holding sites you have since deleted is often the point of having one.

Cairn never writes to the copy.
