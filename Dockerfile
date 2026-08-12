# syntax=docker/dockerfile:1.7

# ── frontend ─────────────────────────────────────────────────────────────
FROM node:22-alpine AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci --no-audit --no-fund 2>/dev/null || npm install --no-audit --no-fund
COPY frontend/ ./
# Vite writes to ../backend/cairn/static, which does not exist in this stage.
RUN mkdir -p /backend/cairn && npm run build -- --outDir /backend/cairn/static --emptyOutDir


# ── python deps ──────────────────────────────────────────────────────────
FROM python:3.12-slim AS deps
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY pyproject.toml ./
COPY backend/cairn/__init__.py backend/cairn/__init__.py
RUN pip install --no-cache-dir .

# pywb serves replay. It is installed only in the image, not declared as a
# dependency of the package: the app never imports it, it only generates
# pywb's config and writes the index it reads, so a development checkout
# stays installable without it.
#
# The setuptools pin is not cosmetic. pywb 2.9.1 — the newest release — still
# does `import pkg_resources`, which setuptools removed in 81. Without the
# pin, `wayback` dies at startup with ModuleNotFoundError and replay is simply
# absent, while everything else in the image works perfectly.
RUN pip install --no-cache-dir "pywb>=2.9,<3" "setuptools<81"

# Playwright drives Chromium for the userscript mint and for interactive
# profiles (docs/06). The Python package goes in the venv here; the browser
# itself lands in the runtime stage, where the apt libraries it needs are.
RUN pip install --no-cache-dir "playwright>=1.49"

# Apprise fans notifications out to whatever somebody already runs (docs/08).
# ntfy and generic webhooks are implemented natively with httpx and need
# nothing; this is what makes every other target work. Image-only for the same
# reason as pywb — the app imports it lazily and reports its absence — so a
# source checkout stays installable without it.
RUN pip install --no-cache-dir "apprise>=1.9"

# yt-dlp downloads the video an archived post embedded — the one thing neither
# wget nor a browser crawler captures. 25 MB, measured.
#
# ffmpeg is deliberately absent. yt-dlp needs it only to *merge* separate video
# and audio streams, and Debian's ffmpeg is 481 MB across 200 packages —
# measured — which is a 28% larger image to raise an archived clip from a muxed
# 720p to a merged 1080p. So the default format asks for a single file that
# needs no merging (services/media.py), and a format string that does require
# merging fails with yt-dlp saying exactly that.
RUN pip install --no-cache-dir "yt-dlp>=2025.1"

# Werkzeug, deliberately above the version pywb asks for.
#
# pywb 2.9.1 declares `werkzeug==2.2.3` — an exact pin, not a floor — and 2.2.3
# carries CVE-2024-34069, the only HIGH the image scan reports. There is no
# pywb release that lifts it, so the choice is to override the pin or ship a
# known-vulnerable dependency forever.
#
# Overridden, because it was measured rather than hoped: pywb replays
# identically on 3.1.8 — byte-for-byte the same responses on the bare content
# (`mp_`), the framed wrapper, the untimestamped redirect and the CDX API,
# against a real capture through a real `wayback`. `test_replay_e2e.py` and
# `test_thumbnail.py` exercise all of that on every container run, so a future
# pywb that genuinely needs 2.2.3 fails a test rather than a user's replay tab.
#
# Last, after every other install, so nothing downstream can resolve it back
# down. pip will print a dependency-conflict warning naming pywb's pin; that
# warning is this comment.
RUN pip install --no-cache-dir "werkzeug>=3.0.3"


# ── runtime ──────────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

ARG S6_OVERLAY_VERSION=3.2.0.2
ARG TARGETARCH

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CAIRN_DATA_DIR=/data \
    CAIRN_CONFIG_DIR=/config \
    CAIRN_PORT=8080 \
    CAIRN_REPLAY_PORT=8081 \
    # Not optional, and the failure it prevents is a nasty one. Playwright
    # otherwise installs the browser under the *building* user's home, while
    # the container runs as `abc` with HOME=/config — a mounted volume. So
    # without this the browser is not missing at build time, it appears to
    # vanish on somebody's fresh install.
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright \
    PUID=99 \
    PGID=100 \
    UMASK=022 \
    S6_BEHAVIOUR_IF_STAGE2_FAILS=2 \
    S6_KEEP_ENV=1

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        wget ca-certificates tzdata curl xz-utils gosu; \
    rm -rf /var/lib/apt/lists/*

# The scope regexes in the wget engine rely on lookahead, which POSIX ERE does
# not have. Fail the build here rather than six hours into a crawl that
# silently either skips every image or crawls image CDNs as websites.
#
# This is a FUNCTIONAL probe, not a version-string check: Debian's wget links
# PCRE2 and honours lookahead perfectly, but reports neither "+pcre" nor
# "-pcre" in its banner (that flag only ever described PCRE1). Grepping the
# banner rejects a perfectly good wget. Compiling the actual pattern is the
# only signal that means anything. The connection to port 1 is expected to
# fail — we only care whether the regex compiled.
RUN set -eux; \
    probe="$(wget --regex-type=pcre \
                  --reject-regex='^https?://example\.com/(?!.*\.jpg$).*$' \
                  --spider --tries=1 --timeout=1 \
                  http://127.0.0.1:1/ 2>&1 || true)"; \
    case "$probe" in \
      *"Invalid regular expression"*|*"Invalid value"*) \
        echo "FATAL: this wget cannot compile a PCRE lookahead."; \
        echo "$probe"; \
        wget --version | head -3; \
        exit 1 ;; \
    esac; \
    echo "wget PCRE lookahead: OK ($(wget --version | head -1))"

# s6-overlay: noarch bundle plus the arch-specific binaries.
RUN set -eux; \
    case "${TARGETARCH:-amd64}" in \
      amd64) S6_ARCH=x86_64 ;; \
      arm64) S6_ARCH=aarch64 ;; \
      *) echo "unsupported arch: ${TARGETARCH}"; exit 1 ;; \
    esac; \
    curl -fsSL "https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}/s6-overlay-noarch.tar.xz" \
      | tar -C / -Jxpf -; \
    curl -fsSL "https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}/s6-overlay-${S6_ARCH}.tar.xz" \
      | tar -C / -Jxpf -

COPY --from=deps /opt/venv /opt/venv

# Chromium, for the userscript mint and interactive profiles.
#
# This is the single largest thing in the image — roughly 1.4 GB once the
# browser and its libraries are in. docs/06 estimated ~500 MB; measured, the
# browser is 389 MB, software GL (libllvm + mesa) is another 169 MB, and the
# CJK and emoji fonts are ~85 MB. The fonts stay: without them a Japanese blog
# renders as tofu boxes in both the mint screenshot and the interactive
# browser, which for an archiving tool is a correctness problem rather than a
# cosmetic one.
#
# `--no-shell` skips the headless shell, a second 262 MB copy of Chromium that
# nothing here uses: the screencast behind interactive profiles runs on the
# full browser, headless.
RUN set -eux; \
    playwright install --with-deps --no-shell chromium; \
    rm -rf /var/lib/apt/lists/*; \
    chmod -R a+rX /opt/playwright

# Prove the browser actually launches, at build time, as a non-root user —
# the same reasoning as the wget PCRE probe above. A browser that is present
# but cannot start turns every mint into a runtime failure with a stack trace
# instead of a build that never shipped.
#
# The sandbox stays on (docs/11). Containers often deny the user namespaces it
# needs, so this is also the check that the requirement is actually met rather
# than assumed; the runtime falls back with a loud warning if it ever is not.
COPY docker/probe-browser.py /tmp/probe-browser.py
RUN set -eux; \
    useradd -u 12345 -m probe; \
    su probe -s /bin/sh -c '/opt/venv/bin/python /tmp/probe-browser.py'; \
    userdel -r probe; \
    rm -f /tmp/probe-browser.py

COPY backend/ /app/backend/
COPY alembic.ini /app/alembic.ini
COPY --from=frontend /backend/cairn/static /app/backend/cairn/static
COPY docker/rootfs/ /

# Stamp the image so the UI can say which build it is running. Placed after
# every COPY on purpose: any source change invalidates this layer and mints a
# new stamp, and an unchanged rebuild reuses it — which is exactly the
# question "am I testing the update?" is asking.
#
# A bare `docker build` still produces a distinct stamp. CI passes the commit:
#     docker build --build-arg CAIRN_BUILD=$(git rev-parse --short HEAD) .
ARG CAIRN_BUILD=
RUN set -eux; \
    now="$(date -u +%Y-%m-%dT%H:%M:%SZ)"; \
    printf '%s\n%s\n' "${CAIRN_BUILD:-img-$(date -u +%y%m%d%H%M)}" "$now" \
      > /app/backend/cairn/BUILD_INFO

ENV PYTHONPATH=/app/backend
WORKDIR /app

RUN chmod -R +x /etc/s6-overlay/s6-rc.d /command 2>/dev/null || true; \
    # 'abc' is the linuxserver convention; PUID/PGID are applied at runtime by
    # the init-perms service, because the values are not known at build time.
    groupadd -g 1000 abc && useradd -u 1000 -g abc -d /config -s /bin/false abc

VOLUME ["/config", "/data"]
EXPOSE 8080 8081

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD curl -fsS "http://localhost:${CAIRN_PORT}/api/health" || exit 1

ENTRYPOINT ["/init"]
