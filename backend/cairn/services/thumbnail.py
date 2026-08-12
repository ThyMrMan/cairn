"""A picture of the archive, not a picture of the site.

docs/05 has asked for a homepage thumbnail on the site card since M1, marked
"needs browser". The browser arrived in M5 and this is the rest of it.

**The obvious implementation photographs the wrong thing.** Pointing Chromium
at the site's live URL is one line and gives a card that lies: the archive of a
blog that closed shows whatever the parked domain serves today, and the archive
of a blog behind a content warning shows the content warning. Site health
monitoring exists precisely because the original goes away — a picture on the
card claiming otherwise would be this tool contradicting its own report. So the
thumbnail is taken of the **replay**, through the same pywb the replay tab
uses, and it is therefore a picture of what is actually in the WARC.

Four things established by running it against the pinned pywb, each of which
changed the code:

  1. **`mp_` does not survive a browser.** Asking pywb for the content without
     its framing wrapper works from `urllib` — the replay end-to-end test has
     relied on that since M3 — but a *browser* loading the same URL at the top
     level ends up at the framed one: measured, `page.url` came back with the
     `mp_` gone. Replayed pages carry a frame check that puts the banner back.

  2. **A screenshot of the framed URL is a photograph of pywb.** Its banner —
     logo, URL bar, "Current Capture" — is the top 90 px of a 800 px viewport,
     measured against 2.9.1. As a thumbnail that is 11% of the card spent
     telling you which software rendered it.

     So the page is loaded inside *our own* iframe, on a blank page we control.
     The frame check sees a framed page and leaves it alone, there is no banner
     to crop, and the archived page gets the whole viewport.

  3. **A URL that is not in the archive renders pywb's "URL Not Found".** That
     is the gated-blog case from docs/04 — a capture holding one redirect and
     nothing else — and it would put an error page on the card of every site
     that most needs looking at. The CDXJ we already keep says whether there is
     a 2xx HTML record before any browser starts.

  4. **Nothing reaches the live web, and the block stays anyway.** The fixture's
     page builds an image URL at runtime, on a host that was never archived;
     with no interception at all, zero requests were made to it — pywb's
     wombat.js rewrites even that. Requests off the replay origin are refused
     regardless, because "wombat rewrote everything we thought to test" is a
     weaker guarantee than not letting the request out, and this runs
     unattended after every capture.

The context carries no profile, no cookie jar and no storage state. It does not
need one — it is reading our own archive — and archived JavaScript is untrusted
code (docs/11), so handing it a session would be handing it to the site.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from cairn.config import Settings
from cairn.db.types import to_iso, utcnow
from cairn.logging import get_logger
from cairn.services import replay, settings_store, storage

log = get_logger(__name__)

THUMB_DIR = "screenshots"
IMAGE_FILE = "home.jpg"
META_FILE = "home.json"
CONTENT_TYPE = "image/jpeg"

ENABLED_SETTING = "thumbnails.enabled"

# A full-sized viewport rendered at half resolution: the page lays out as it
# would on a desktop, and the image comes out 640x400. Rendering into a small
# viewport instead would trip every responsive layout into its phone form,
# which is not what the archive looks like.
VIEWPORT = {"width": 1280, "height": 800}
SCALE = 0.5
QUALITY = 75

REPLAY_PROBE_TIMEOUT_S = 3
NAV_TIMEOUT_MS = 20_000
# Long enough for webfonts and the images the page asks for on load, short
# enough that a page which never settles still yields something.
SETTLE_MS = 1_200


class ThumbnailError(RuntimeError):
    """The picture could not be taken. The message is shown to a person."""


@dataclass(frozen=True, slots=True)
class Shot:
    url: str
    timestamp: str
    bytes: int

    def to_dict(self) -> dict[str, Any]:
        return {"url": self.url, "timestamp": self.timestamp, "bytes": self.bytes}


# ── where it lives ───────────────────────────────────────────────────────


def thumb_dir(settings: Settings, archive_path: str) -> Path:
    return storage.site_dir(settings, archive_path) / storage.DERIVED_DIR / THUMB_DIR


def image_path(settings: Settings, archive_path: str) -> Path:
    return thumb_dir(settings, archive_path) / IMAGE_FILE


def exists(settings: Settings, archive_path: str) -> bool:
    """Whether this site has a thumbnail on disk.

    A stat rather than a column. The image is derived data that a folder move,
    a restore from backup or a hand-deleted directory can change without the
    database hearing about it, and a boolean that can disagree with the disk is
    a broken image on a card with no way to clear it.
    """
    try:
        return image_path(settings, archive_path).is_file()
    except (OSError, ValueError):  # pragma: no cover — unreadable archive root
        return False


def describe(settings: Settings, archive_path: str) -> dict[str, Any] | None:
    """What the picture is of: the URL, the capture's timestamp, when taken."""
    path = thumb_dir(settings, archive_path) / META_FILE
    if not path.is_file():
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def enabled(session: Session) -> bool:
    return bool(settings_store.get(session, ENABLED_SETTING, True))


# ── what to photograph ───────────────────────────────────────────────────


def subject(settings: Settings, archive_path: str, seeds: list[str]) -> replay.CdxRecord | None:
    """The newest archived page worth putting on the card.

    Each seed in turn, newest first, because a multi-seed site's identity is
    its first seed and the others are the domains it also lives on. The
    fallback matters more than it looks: a site created by the URL importer is
    seeded at an origin whose `/` was never fetched — only the pasted pages
    were — so insisting on the homepage would leave exactly those sites blank
    forever. The index is in SURT order, so the first HTML record is the
    shortest path on the alphabetically first host: the nearest thing to a
    front page that the archive actually holds.
    """
    for seed in seeds:
        record = _newest_page(replay.lookup(settings, archive_path, seed))
        if record is not None:
            return record
    return _first_page(settings, archive_path)


def _is_page(record: replay.CdxRecord) -> bool:
    status = str(record.status or "")
    mime = (record.mime or "").lower()
    # An empty mime is treated as a page for the same reason `replayable_pages`
    # does: some engines record none, and refusing those would blank the card
    # for a whole engine's worth of captures.
    return status.startswith("2") and ("html" in mime or not mime)


def _newest_page(records: list[replay.CdxRecord]) -> replay.CdxRecord | None:
    pages = [r for r in records if _is_page(r)]
    return max(pages, key=lambda r: r.timestamp) if pages else None


def _first_page(settings: Settings, archive_path: str) -> replay.CdxRecord | None:
    for record in replay.index_records(settings, archive_path):
        if _is_page(record):
            return record
    return None


def is_from_capture(record: replay.CdxRecord, capture_dir: str) -> bool:
    """Whether that record was written by this capture.

    The index's `filename` is site-relative — `captures/<dir>/warc/…` — which
    is what makes this answerable without opening a WARC. It is the whole of
    the "should this capture take a new picture?" decision: an incremental
    capture of one new post has not changed what the front page looks like, and
    relaunching Chromium after every feed poll to re-photograph an unchanged
    page is a browser start per post for no new information.
    """
    return record.filename.replace("\\", "/").startswith(f"{storage.CAPTURES_DIR}/{capture_dir}/")


# ── taking it ────────────────────────────────────────────────────────────


def replay_ready(settings: Settings) -> tuple[bool, str]:
    """Whether the pywb next door is answering.

    Checked before a browser is launched so the reason is "replay is not
    running" rather than a navigation timeout, which points at the wrong half.
    """
    origin = settings.replay_internal_origin
    try:
        # S310 is about schemes: this URL is `http://127.0.0.1:<configured port>/`
        # built here from our own settings, with nothing user-supplied in it.
        with urllib.request.urlopen(f"{origin}/", timeout=REPLAY_PROBE_TIMEOUT_S):  # noqa: S310
            return True, ""
    except urllib.error.HTTPError:
        # Answering, with an opinion. pywb serves 404 for an unknown path and
        # that is still pywb being up.
        return True, ""
    except OSError as exc:
        return False, f"replay is not answering on {origin} ({exc.__class__.__name__})"


def capture_site(
    settings: Settings,
    *,
    site_id: int,
    archive_path: str,
    seeds: list[str],
    record: replay.CdxRecord | None = None,
) -> Shot:
    """Photograph a site's archived front page. Raises ThumbnailError.

    Takes identifiers rather than a `Site`: this hands control to an event loop
    and back, and an ORM instance that crosses that boundary is one detached
    object away from a confusing failure at an unrelated call site.
    """
    import asyncio

    from cairn.services import browser

    chosen = record or subject(settings, archive_path, seeds)
    if chosen is None:
        raise ThumbnailError(
            "this archive holds no page that replay could show — a capture that was "
            "redirected to a content warning has nothing to photograph"
        )

    ok, reason = browser.availability()
    if not ok:
        raise ThumbnailError(reason)
    ready, reason = replay_ready(settings)
    if not ready:
        raise ThumbnailError(reason)

    origin = settings.replay_internal_origin
    url = f"{origin}/{replay.collection_name(site_id)}/{chosen.timestamp}mp_/{chosen.url}"
    image = asyncio.run(_shoot(origin, url))

    target = image_path(settings, archive_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    storage.write_atomic(target, image)
    storage.write_atomic(
        target.parent / META_FILE,
        json.dumps(
            {"url": chosen.url, "timestamp": chosen.timestamp, "taken_at": to_iso(utcnow())},
            indent=2,
        ),
    )
    return Shot(url=chosen.url, timestamp=chosen.timestamp, bytes=len(image))


_WRAPPER = (
    "<!doctype html><meta charset=utf-8>"
    "<style>html,body{margin:0;padding:0;overflow:hidden;background:#fff}"
    "iframe{border:0;display:block;width:100vw;height:100vh}</style>"
)


async def _shoot(origin: str, url: str) -> bytes:
    from cairn.services import browser

    async with (
        browser.launched() as launched,
        browser.context(launched, viewport=VIEWPORT, device_scale_factor=SCALE) as context,
    ):
        page = await context.new_page()
        page.set_default_timeout(NAV_TIMEOUT_MS)

        async def only_replay(route: Any) -> None:
            if route.request.url.startswith(origin):
                await route.continue_()
            else:
                await route.abort()

        await page.route("**/*", only_replay)
        await page.set_content(_WRAPPER)
        # The URL is built from an archived URL, so it never touches markup:
        # passed as an argument it cannot close an attribute, whatever the site
        # put in its own links.
        await page.evaluate(
            "(src) => { const f = document.createElement('iframe');"
            " f.src = src; document.body.appendChild(f); }",
            url,
        )

        handle = await page.wait_for_selector("iframe", timeout=NAV_TIMEOUT_MS)
        frame = await handle.content_frame() if handle else None
        if frame is None:  # pragma: no cover — the element was just created
            raise ThumbnailError("the replay frame never appeared")
        try:
            await frame.wait_for_load_state("load", timeout=NAV_TIMEOUT_MS)
        except Exception as exc:
            raise ThumbnailError(f"the archived page did not load: {_first_line(exc)}") from exc
        await page.wait_for_timeout(SETTLE_MS)

        shot = await page.screenshot(type="jpeg", quality=QUALITY)
        return bytes(shot)


def _first_line(exc: Exception) -> str:
    return str(exc).strip().splitlines()[0][:200]
