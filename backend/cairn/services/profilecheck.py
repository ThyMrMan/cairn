"""Asking a site whether a browsertrix profile still works.

There has been a Test button for cookie profiles since M1: it fetches the
verify URL with the jar and runs the interstitial detector over the answer.
There was none for a *browser* profile — the one kind that cannot re-mint
itself, and therefore the one where "is it still good?" is a question somebody
actually has to ask.

Diagnosing that without this took four rounds of reading a finished capture,
and the finished capture is the expensive way to find out.

**It has to be browsertrix, not httpx.** `profiles.verify` deliberately uses
plain HTTP, because for wget the question is what *wget* will get. Here the
question is what the crawler will get, and the crawler is a different browser
reading an encrypted profile it alone can decrypt. A jar-based check would
answer confidently about something else.

**One page, and the crawl is the cheap part.** A page limit of 1 with
`--scopeType page` means no links are followed: it loads the verify URL,
records it, and exits. The expensive part is starting Chromium, which is why
this is a button rather than something run on a schedule.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cairn.logging import get_logger

log = get_logger(__name__)

COLLECTION = "profilecheck"
PROFILE_MOUNT = "/cairn/auth"
CRAWLS = "/crawls"
# A gated page answers in seconds; this bounds a site that never responds.
TIMEOUT_S = 180
PAGE_TIMEOUT_S = 45


@dataclass(slots=True)
class CheckResult:
    """What the crawler saw, in the three shapes that need different fixes."""

    #: pass | gate | no_profile | error
    verdict: str = "error"
    reason: str = ""
    final_url: str = ""
    status: int = 0
    bytes: int = 0
    #: Whether the crawler reported loading the profile at all. A false here
    #: with a `gate` verdict is a different bug from a true one: the first is
    #: a file that did not arrive, the second a session the site rejected.
    profile_loaded: bool = False
    log_tail: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.verdict == "pass"

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "verdict": self.verdict,
            "reason": self.reason,
            "final_url": self.final_url,
            "status": self.status,
            "bytes": self.bytes,
            "profile_loaded": self.profile_loaded,
            "log_tail": self.log_tail,
        }


async def check(
    tarball: Path,
    verify_url: str,
    *,
    image: str,
    work_root: Path,
    user_agent: str = "",
) -> CheckResult:
    """Load one page in the crawler's own browser with this profile.

    `tarball` is the unsealed profile, already on disk somewhere the container
    can be given. The caller owns it and its directory — this writes only into
    a temp tree of its own, under `work_root`.

    **`work_root` must be inside a mounted volume, and that is not a detail.**
    The crawl tree is handed to a *sibling* container, so the daemon has to be
    able to resolve it: a path only this process can see cannot be mounted.
    The system temp directory is inside the image's writable layer on a normal
    deployment, so defaulting to it produced "is not inside any mounted
    volume" on an instance where /data and /config were both mounted correctly
    — a confusing error, because nothing was wrong with the deployment.
    """
    from cairn.services import containers

    ok, reason = containers.available()
    if not ok:
        return CheckResult(verdict="error", reason=reason)
    if not tarball.is_file():
        return CheckResult(
            verdict="no_profile",
            reason="There is no browser profile stored on this access profile.",
        )

    work_root.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(dir=work_root, prefix="profilecheck-"))
    container: str | None = None
    try:
        argv = [
            "crawl",
            "--collection",
            COLLECTION,
            "--url",
            verify_url,
            # One page, no links followed. The question is about this URL.
            "--scopeType",
            "page",
            "--limit",
            "1",
            "--workers",
            "1",
            "--timeout",
            str(PAGE_TIMEOUT_S),
            "--profile",
            f"{PROFILE_MOUNT}/{tarball.name}",
        ]
        if user_agent:
            argv += ["--userAgent", user_agent]

        run = containers.RunSpec(
            image=image,
            argv=argv,
            mounts=[(work, CRAWLS), (tarball.parent, PROFILE_MOUNT)],
            job_id=0,
            shm_size="2g",
            memory="",
            network="",
        )
        async with containers.client() as http:
            if not await containers.image_present(http, image):
                return CheckResult(
                    verdict="error",
                    reason=(
                        f"{image} is not pulled yet. Run a capture with this engine once "
                        "first — the image is about a gigabyte and this check will not "
                        "download it."
                    ),
                )
            container = await containers.create(http, run)
            await containers.start(http, container)
            # `containers.wait` blocks until the container exits, and a
            # site that never answers would hold the request open forever.
            await asyncio.wait_for(containers.wait(http, container), TIMEOUT_S)
            text = await containers.logs_text(http, container)
    except Exception as exc:  # pragma: no cover — docker refusing
        return CheckResult(verdict="error", reason=f"the check could not run: {exc}")
    finally:
        if container is not None:
            with _quiet():
                async with containers.client() as http:
                    await containers.remove(http, container)

    result = _read_result(work, verify_url)
    result.profile_loaded = "With Browser Profile" in text
    result.log_tail = [line for line in text.splitlines() if '"logLevel":"error"' in line][-5:]
    if result.verdict == "gate" and not result.profile_loaded:
        result.verdict = "no_profile"
        result.reason = (
            "The crawler never loaded the profile, so it browsed signed out. "
            "The tarball did not reach the container — re-upload it."
        )
    shutil.rmtree(work, ignore_errors=True)
    return result


def _read_result(work: Path, verify_url: str) -> CheckResult:
    """The verdict, from the record the crawler actually wrote.

    Read out of the WARC rather than off the log, and that is the point of
    doing it this way: the crawler exits 0 having archived a content warning,
    so its own status says nothing. The bytes it stored are the answer.
    """
    from warcio.archiveiterator import ArchiveIterator

    from cairn.services import interstitial

    archive = work / "collections" / COLLECTION / "archive"
    if not archive.is_dir():
        return CheckResult(
            verdict="error", reason="the crawler wrote no archive — see the log below"
        )

    best: CheckResult | None = None
    for warc in sorted(archive.glob("*.warc.gz")):
        try:
            with warc.open("rb") as fh:
                for record in ArchiveIterator(fh):
                    if record.rec_type != "response":
                        continue
                    url = record.rec_headers.get_header("WARC-Target-URI") or ""
                    ctype = (record.http_headers.get_header("Content-Type") or "").lower()
                    if "html" not in ctype:
                        continue
                    status = int(record.http_headers.get_statuscode() or 0)
                    body = record.content_stream().read(512 * 1024)
                    verdict = interstitial.looks_blocked(body, url)
                    candidate = CheckResult(
                        verdict="gate" if verdict.blocked else "pass",
                        reason=(
                            verdict.reason
                            if verdict.blocked
                            else f"real content from {url} ({len(body)} bytes)"
                        ),
                        final_url=url,
                        status=status,
                        bytes=len(body),
                    )
                    # The page asked for wins over anything else in the WARC —
                    # a gated blog also records the gate at its own URL, and
                    # answering about that record instead would report a
                    # failure for a profile that worked.
                    if url.rstrip("/") == verify_url.rstrip("/"):
                        return candidate
                    if best is None or (best.verdict == "pass" and candidate.verdict == "gate"):
                        best = candidate
        except Exception as exc:  # pragma: no cover — truncated WARC
            log.warning("profile check could not read a WARC", extra={"err": str(exc)})
            continue

    if best is None:
        return CheckResult(
            verdict="error", reason="the crawler archived no HTML — see the log below"
        )
    return best


def _quiet() -> Any:
    import contextlib

    return contextlib.suppress(Exception)
