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
COPY backend/ /app/backend/
COPY alembic.ini /app/alembic.ini
COPY --from=frontend /backend/cairn/static /app/backend/cairn/static
COPY docker/rootfs/ /

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
