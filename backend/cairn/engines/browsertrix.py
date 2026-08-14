"""The `browsertrix` engine: `python -m cairn.engines.browsertrix <job.json>`.

The second real engine, and the one that answers everything wget cannot: a
page whose content is built by script, a link that only exists after that
script runs, an image whose `src` is set when it scrolls into view. Measured
against a fixture doing all three — wget archives the shell and nothing else.

**It is an adapter, not a `runtime: docker` engine.** docs/05 sketched running
the stock image directly with templated arguments, and that cannot work: the
protocol says an engine writes cairn NDJSON on stdout, and browsertrix writes
its own JSON log format. Something has to translate, and putting that inside
the generic docker runtime would give the runtime an "and which log format?"
field — which is where a clean seam turns into a pile of special cases. So the
runtime type stays honest (your image speaks the protocol), and a tool that
does not speak it gets an adapter like this one, which is also the worked
example a third party copies.

Four things established by running it rather than reading:

**Its stdout carries no per-URL record.** The logs report pages started and
finished plus a periodic `crawlStatus`; the complete list of what was archived
is only in the CDXJ it writes at the end. So `url` events come from that file
after the crawl rather than streaming during it.

**Its output layout is not ours.** It writes
`collections/<name>/archive/*.warc.gz`, and a capture keeps WARCs in `warc/` —
which is what `site_warcs` globs and therefore what ever reaches replay.

**Leave `--behaviors` alone.** Its default is
`autoplay,autofetch,autoscroll,siteSpecific`, and the first attempt here
passed a shorter list that dropped `autofetch` — the behavior that fetches
lazily-referenced resources, which is most of the reason to use this engine.
The lazy-image fixture went from three images to one.

**It cannot take a cookie jar, and the bridge is a tarball, not a jar.**
`crawl --help` has no cookie option at all; `--profile` takes a tar.gz of a
browser profile. A profile built with our own Chromium is *accepted* and its
cookies are ignored, because browsertrix runs Brave and we ship Google Chrome
for Testing — a different browser, so a different cookie-encryption key.
Verified end to end against a gated fixture: the crawl archived the
interstitial.

M7 concluded from that that no bridge existed, and that was one step short.
The crawler's own image ships `create-login-profile`, which drives *the same
browser* and writes a tarball this accepts — so the mismatch is not between
browsertrix and profiles, it is between browsertrix and profiles built
elsewhere. Two things follow, both measured against 1.14.1:

- It runs **headful under Xvfb** (`--headless` defaults to false, and both
  Xvfb and x11vnc are in the image). That is why it gets through sign-ins the
  CDP screencast in docs/06 cannot: the headless fingerprint is simply absent.
  Confirmed against a real Google login, which is the case docs/06 records as
  out of reach.
- The interactive session commits over its own control API on port 9223 —
  `POST /createProfile` writes `/crawls/profiles/profile.tar.gz` and nothing
  in the VNC window says so. Measured at 41 MB for one Google login.

Cairn does not run that tool; it takes the tarball, seals it, and hands it
back through `--profile`. A cookie jar with no tarball still gets the warning,
because for this engine a jar really is unusable.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import signal
import sys
import time
from pathlib import Path
from types import FrameType
from typing import Any

import httpx

from cairn.engines.protocol import EventWriter, JobSpec
from cairn.services.scope import Scope

# Where browsertrix keeps its working tree inside its own container.
CRAWLS = "/crawls"
COLLECTION = "capture"

# Where the job's temp directory appears to the crawler when a browser profile
# is being passed. Deliberately not under /crawls: that tree is the crawler's
# to write, and this is one file it only reads.
PROFILE_MOUNT = "/cairn/auth"

DEFAULT_IMAGE = "webrecorder/browsertrix-crawler:1.14.1"

# What browsertrix can be told a crawl boundary is. Cairn's scope is richer —
# per-host crawl and asset decisions — so an explicit host allowlist is passed
# as `custom` plus an include regex instead.
SCOPE_TYPES = ("page", "page-spa", "prefix", "host", "domain", "any")


class Runner:
    def __init__(self, spec: JobSpec, events: EventWriter) -> None:
        self.spec = spec
        self.events = events
        self.out = Path(spec.output_dir)
        self.tmp = Path(spec.temp_dir)
        self.config: dict[str, Any] = dict(spec.config or {})
        self.container: str | None = None
        self.terminating = False
        self.pages = 0
        self.failed = 0
        self.urls = 0

    # ── lifecycle ────────────────────────────────────────────────────────

    def request_stop(self, _signum: int, _frame: FrameType | None) -> None:
        """SIGTERM: stop the container, letting it close its WARC.

        The same contract as the wget engine — a cancelled capture costs a
        partial archive rather than a truncated one.
        """
        if self.terminating:
            return
        self.terminating = True
        self.events.log("cancellation requested; stopping the crawler", level="warning")

    def run(self) -> int:
        return asyncio.run(self._run())

    async def _run(self) -> int:
        from cairn.services import containers

        ok, reason = containers.available()
        if not ok:
            self.events.result("failed", {}, error=reason)
            return 1

        (self.out / "warc").mkdir(parents=True, exist_ok=True)
        work = self.tmp / "crawls"
        work.mkdir(parents=True, exist_ok=True)

        image = str(self.config.get("image") or DEFAULT_IMAGE)
        started = time.monotonic()
        self.events.started(tool_version=image)
        self._warn_about_auth()

        code = 1
        async with containers.client() as http:
            try:
                await self._ensure_image(http, image)
                code = await self._crawl(http, image, work, started)
            except containers.ContainerError as exc:
                self.events.result("failed", self._stats(started), error=str(exc))
                return 1
            except httpx.HTTPError as exc:
                # The socket went away, or was never openable in the first
                # place. Reaching here means the preflight missed it, and a
                # job log is no place for an httpx traceback — say what broke
                # and re-run the preflight to say why.
                _, reason = containers.available()
                self.events.result(
                    "failed",
                    self._stats(started),
                    error=reason or f"lost contact with the Docker socket: {exc}",
                )
                return 1

        warcs = self._collect(work)
        stats = {**self._stats(started), "warcs": warcs}

        if self.terminating:
            self.events.result("partial", stats, error="cancelled")
            return 0
        if code == 0 and warcs:
            self.events.result("ok", stats)
            return 0
        if warcs:
            # Some pages failed and the rest are archived — the same judgement
            # the wget engine makes: an archive with known gaps is not a failed
            # capture (docs/05).
            self.events.result("partial", stats, error=f"the crawler exited {code}")
            return 0
        self.events.result(
            "failed", stats, error=f"the crawler exited {code} without archiving anything"
        )
        return code or 1

    def _profile_tarball(self) -> Path | None:
        """The browser profile to crawl with, if the job carries one.

        Checked for existence rather than trusted: the supervisor writes it
        into the temp directory, and a `--profile` pointing at nothing makes
        browsertrix start a clean browser and archive the login page, which is
        the failure this is here to prevent.
        """
        named = self.spec.auth.profile_file
        if not named:
            return None
        path = Path(named)
        return path if path.is_file() else None

    def _warn_about_auth(self) -> None:
        """Say before the crawl what will and will not get past a gate.

        Otherwise a site behind a content warning is archived as several
        thousand copies of the content warning, and the only sign is that the
        pages look wrong when somebody eventually reads them.
        """
        if self._profile_tarball() is not None:
            self.events.log("crawling with the profile's browsertrix browser profile")
            return
        if self.spec.auth.profile_file:
            self.events.warning(
                "auth_unsupported",
                "This site's profile names a browsertrix browser profile that is not on "
                "disk, so the crawl is starting from a clean browser and anything behind "
                "the gate will be archived as the gate. Re-upload the tarball.",
            )
            return
        if not self.spec.auth.cookies_file:
            return
        self.events.warning(
            "auth_unsupported",
            "This site has a cookie-based access profile, and browsertrix cannot use one — "
            "it has no cookie option, and it runs a different browser from the one that "
            "minted the jar, so cookies do not carry across. Anything behind the gate will "
            "be archived as the gate. Either use the wget engine, or attach a browsertrix "
            "browser profile to this access profile — `create-login-profile` in the "
            "crawler's own image builds one that it will accept.",
        )

    def _stats(self, started: float) -> dict[str, Any]:
        return {
            "urls": self.urls or self.pages,
            "pages": self.pages,
            "errors": self.failed,
            "duration_s": round(time.monotonic() - started, 1),
        }

    async def _ensure_image(self, http: Any, image: str) -> None:
        from cairn.services import containers

        if await containers.image_present(http, image):
            return
        self.events.log(f"pulling {image} — about a gigabyte, and only the first time")
        async for status in containers.pull(http, image):
            self.events.log(status, level="debug")

    # ── the crawl ────────────────────────────────────────────────────────

    async def _crawl(self, http: Any, image: str, work: Path, started: float) -> int:
        from cairn.services import containers

        mounts = [(work, CRAWLS)]
        if self._profile_tarball() is not None:
            # Its own directory, read by the crawler and nothing else. The
            # temp directory it sits in also holds the cookie jar, and there
            # is no reason to show that to a container that cannot read one.
            mounts.append((self.tmp, PROFILE_MOUNT))

        run = containers.RunSpec(
            image=image,
            argv=self._argv(),
            mounts=mounts,
            job_id=self.spec.job_id,
            shm_size=str(self.config.get("shm_size") or "2g"),
            memory=str(self.config.get("memory_limit") or ""),
            network=str(self.config.get("docker_network") or ""),
        )
        self.container = await containers.create(http, run)
        await containers.start(http, self.container)
        self.events.log("crawler started")

        try:
            await self._pump(http, self.container)
            code = await containers.wait(http, self.container)
        finally:
            if self.terminating:
                await containers.stop(http, self.container)
            await self._save_log(http, self.container)
            await containers.remove(http, self.container)
        return code

    async def _pump(self, http: Any, container: str) -> None:
        """Turn browsertrix's log stream into cairn events while it runs."""
        from cairn.services import containers

        buffer = b""
        last_progress = 0.0
        async for stream, chunk in containers.stream_logs(http, container):
            if self.terminating:
                await containers.stop(http, container)
                break
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                self._handle(line.decode("utf-8", errors="replace").strip(), stream)
            now = time.monotonic()
            if now - last_progress >= 2.0:
                last_progress = now
                self.events.progress(
                    self.pages, unit="pages", bytes=_warc_bytes(self.tmp / "crawls")
                )

    def _handle(self, text: str, stream: int) -> None:
        if not text:
            return
        if not text.startswith("{"):
            # Node's own warnings arrive on stderr as plain text. Kept at
            # debug so a real message is still findable among them.
            self.events.log(text[:500], level="debug" if stream == 2 else "info")
            return
        try:
            entry = json.loads(text)
        except ValueError:
            self.events.log(text[:500])
            return
        if not isinstance(entry, dict):  # pragma: no cover — defensive
            return

        context = str(entry.get("context") or "")
        message = str(entry.get("message") or "")
        raw = entry.get("details")
        details: dict[str, Any] = raw if isinstance(raw, dict) else {}
        level = {"error": "error", "warn": "warning", "fatal": "error"}.get(
            str(entry.get("logLevel") or "info"), "info"
        )

        if context == "crawlStatus":
            self.pages = int(details.get("crawled") or self.pages)
            self.failed = int(details.get("failed") or self.failed)
            total = details.get("total")
            # Pages, not URLs, and said so. Its stdout carries no per-URL
            # record at all — the archived list only exists in the CDXJ it
            # writes at the end — so this counter cannot be made to mean what
            # wget's means. Labelling both "URLs" made a capture look 20x
            # slower than the one before it when the real change was that a
            # few hundred label pages had stopped being crawled.
            self.events.progress(self.pages, unit="pages", total=int(total) if total else None)
            return
        if context == "worker" and message == "Starting page":
            self.events.log(f"page: {details.get('page')}")
            return
        if level in ("error", "warning"):
            where = details.get("page") or details.get("url") or ""
            self.events.log(f"{message}{f' ({where})' if where else ''}", level=level)
            return
        self.events.log(message, level="debug")

    def _argv(self) -> list[str]:
        """browsertrix's command line, from cairn's scope and config.

        Never a shell string: seeds, hostnames and regexes are all
        user-controlled and go straight into an argument list.

        Two of its flags read differently from how they look. `--include` and
        `--exclude` each take **one** regex rather than repeating, and using
        `--include` at all requires `--scopeType custom`.
        """
        scope = Scope.from_dict(self.spec.scope)
        seeds = self.spec.seeds or scope.seeds
        crawlable = [rule.host for rule in scope.hosts if rule.crawl_pages]

        argv = [
            "crawl",
            "--collection",
            COLLECTION,
            "--workers",
            str(int(self.config.get("workers", 1))),
            # Its own CDXJ is where the url events come from. Ours is rebuilt
            # across every capture by the cdxj-index post-processor.
            "--generateCDX",
            "--postLoadDelay",
            str(int(self.config.get("post_load_delay_s", 2))),
            "--pageExtraDelay",
            str(int(self.config.get("page_extra_delay_s", 0))),
            "--timeout",
            str(int(self.config.get("page_timeout_s", 90))),
        ]
        for seed in seeds:
            argv += ["--url", seed]

        if crawlable:
            argv += ["--scopeType", "custom", "--include", _host_alternation(crawlable)]
        else:  # pragma: no cover — Scope.validate requires a crawlable host
            argv += ["--scopeType", _scope_type(self.config.get("scope_type"), scope)]

        # wget obeys robots.txt unless told otherwise; browsertrix ignores it
        # unless told to. So `obey_robots` — which defaults to true — meant
        # nothing here, and a scope setting that constrains one engine and is
        # silently inert in another is worse than no setting at all.
        #
        # Reported: a 43-post Blogger blog whose crawl hit its cap, 115 of the
        # pages being `/search/label/*`. Blogger disallows `/search` in
        # robots.txt, the preset says so and leaves those pages to it, and
        # that arrangement quietly stopped holding the moment the engine
        # changed.
        if scope.obey_robots:
            argv += ["--useRobots"]

        if scope.reject_patterns:
            combined = "|".join(f"(?:{p})" for p in scope.reject_patterns)
            # Both, because they cover different halves and `--exclude` alone
            # covers the wrong one for most of these patterns.
            #
            # `--exclude` is documented as "regex of **page URLs**": it filters
            # the crawl queue. A beacon fired by the page's own JavaScript is
            # never queued as a page, so no exclude rule can touch it —
            # measured, and painfully: `/b/stats` stayed at 26% of all fetches
            # across three captures with an exclude pattern that matched it
            # perfectly. `--blockRules` is the network-level one, and a plain
            # string there is read as a URL regex with type "block".
            #
            # This also makes the two engines agree. wget's `--reject-regex`
            # has always applied to everything it fetches, so "skip URLs
            # matching" meant one thing on one engine and something much
            # weaker on the other.
            argv += ["--exclude", combined, "--blockRules", combined]
        if scope.max_pages:
            argv += ["--limit", str(scope.max_pages)]
        if scope.max_depth is not None:
            argv += ["--depth", str(scope.max_depth)]
        if scope.max_bytes:
            argv += ["--sizeLimit", str(scope.max_bytes)]

        # Left at the tool's own default unless somebody asks for otherwise.
        if behaviors := str(self.config.get("behaviors") or "").strip():
            argv += ["--behaviors", behaviors]
        if agent := self.spec.auth.user_agent:
            argv += ["--userAgent", agent]
        if (tarball := self._profile_tarball()) is not None:
            argv += ["--profile", f"{PROFILE_MOUNT}/{tarball.name}"]
        if self.config.get("extract_text", True):
            argv += ["--text", "to-warc"]
        return argv

    # ── collecting what it produced ──────────────────────────────────────

    def _collect(self, work: Path) -> int:
        """Move the WARCs into the capture, and report what was archived.

        Moved rather than copied: they are already on the same filesystem, and
        a capture that silently doubles its own size on the way out is a poor
        first impression.
        """
        collection = work / "collections" / COLLECTION
        target = self.out / "warc"
        moved = 0
        for warc in sorted((collection / "archive").glob("*.warc.gz")):
            destination = target / warc.name
            try:
                shutil.move(str(warc), str(destination))
            except OSError as exc:  # pragma: no cover — permissions
                self.events.log(f"could not move {warc.name}: {exc}", level="warning")
                continue
            moved += 1
            self.events.artifact("warc", f"warc/{warc.name}", size=destination.stat().st_size)

        self._emit_urls(collection / "indexes" / "index.cdxj")
        return moved

    def _emit_urls(self, cdxj: Path) -> None:
        """One `url` event per archived record, from browsertrix's own CDXJ.

        Its `urn:pageinfo:` entries are per-page metadata records rather than
        anything that was fetched, so passing them through would put rows in
        the capture's URL list for URLs that never existed.
        """
        if not cdxj.is_file():
            self.events.log("the crawler wrote no index, so no URL list is available", "warning")
            return
        for line in cdxj.read_text(encoding="utf-8", errors="replace").splitlines():
            parts = line.split(" ", 2)
            if len(parts) < 3 or parts[0].startswith("urn:"):
                continue
            try:
                record = json.loads(parts[2])
            except ValueError:  # pragma: no cover — malformed line
                continue
            url = str(record.get("url") or "")
            if not url:
                continue
            status = str(record.get("status") or "")
            if status.isdigit() and int(status) >= 400:
                self.failed += 1
            self.events.url(
                url,
                status=int(status) if status.isdigit() else None,
                mime=record.get("mime"),
                size=_int_or_none(record.get("length")),
                digest=record.get("digest"),
            )
            self.urls += 1

    async def _save_log(self, http: Any, container: str) -> None:
        """Keep the crawler's own log beside the capture, as wget's is kept."""
        from cairn.services import containers

        text = await containers.logs_text(http, container)
        if text:
            (self.out / "crawl.log").write_text(text, encoding="utf-8")


def _scope_type(configured: Any, scope: Scope) -> str:
    if str(configured) in SCOPE_TYPES:
        return str(configured)
    # `prefix` is browsertrix's own default and the right one for a blog:
    # everything under the seed's path, nothing above it.
    return "page" if scope.max_depth == 0 else "prefix"


def _host_alternation(hosts: list[str]) -> str:
    """An include regex matching exactly these hosts.

    `www\\d*\\.` is allowed in front because browsertrix's own scope regexes do
    the same, and a blog that redirects between the bare and www forms would
    otherwise leave the crawl at the first hop.
    """
    joined = "|".join(re.escape(host) for host in hosts)
    return rf"^https?://(?:www\d*\.)?(?:{joined})(?::\d+)?/"


def _warc_bytes(root: Path) -> int:
    total = 0
    if not root.is_dir():
        return 0
    for path in root.rglob("*.warc.gz"):
        try:
            total += path.stat().st_size
        except OSError:  # pragma: no cover — written while being counted
            continue
    return total


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if not args:
        print("usage: python -m cairn.engines.browsertrix <job.json>", file=sys.stderr)
        return 2

    events = EventWriter()
    runner = Runner(JobSpec.load(args[0]), events)
    signal.signal(signal.SIGTERM, runner.request_stop)
    signal.signal(signal.SIGINT, runner.request_stop)
    return runner.run()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
