"""The archive gallery: one static page that is the front door to replay.

pywb's own landing page is an application — a list of collection IDs
(`site-42`), and inside each a search box that asks you to type a URL. It has
no titles, no screenshots, and it needs JavaScript to render at all, which is
exactly the wrong thing for the sites that only replay with scripts off
(docs/07). So browsing straight to pywb is unpleasant enough that the app's own
UI becomes the only comfortable way in.

This module generates the page that should sit in front of pywb instead: a
self-contained `index.html` that is the **replay origin's** landing page,
served there by the reverse proxy (docs/10). It is a filterable grid of
homepage screenshots, one card per site, each linking to that site's newest
capture. It is derived data in the same sense the collection tree is —
regenerable from the database and the archives at any time, and rebuilt at
`replay-init`, after every capture, and when a site is deleted or restored.

Five things shape it, each a decision made before any code:

  1. **It is static, and JavaScript only enhances it.** Every card is a real
     `<a>` in the file. The filter box and the sort control are the only
     script, so the page works — and first-paints — with JavaScript disabled,
     which is the state of the very reader who turned it off to tame a site
     whose archived scripts spin.

  2. **The screenshot and the destination are the same capture.** A card links
     to the exact `(url, timestamp)` the thumbnail was taken of — read from the
     site's `derived/screenshots/home.json` — so clicking the picture opens the
     page in the picture, with no drift if a newer capture has not been
     re-photographed.

  3. **Cards open the mini-viewer, degrading to bare replay.** With JavaScript
     on, a card's href is rewritten to `view.html`, which frames the capture
     and offers a scripts-on/off toggle (the app's sandbox trick, reimplemented
     as a static page on the replay origin). With JavaScript off, the href is
     left as the bare `mp_` URL, which renders without scripts — the plain link
     is the fallback, and needs no `<noscript>` special-casing.

  4. **Images are inlined.** Each `home.jpg` is embedded as a base64 `data:`
     URI, so the whole gallery is one portable file that also browses over SMB
     with the app stopped. Reused as-is rather than re-encoded — fine at the
     single-user scale this tool is built for; a downscaled variant is the
     thing to add if a gallery ever grows past a few hundred sites.

  5. **A site with nothing to show still appears.** A gated blog whose only
     capture is a redirect, or a site not yet captured, gets a placeholder tile
     that says so rather than being hidden — the gallery should not quietly
     omit part of the archive, which is the same stance the thumbnail service
     takes about photographing a page that is not there.

The HTML, CSS and JavaScript live in `gallery_assets/` beside this module
rather than as string literals: they are a web page, edited as one, and keeping
them out of Python keeps both readable.
"""

from __future__ import annotations

import base64
import functools
import html
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from urllib.parse import quote, urlsplit

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cairn.config import Settings
from cairn.db.models import Capture, Feed, Folder, Site, SiteTag, Tag
from cairn.db.types import to_iso, utcnow
from cairn.logging import get_logger
from cairn.services import replay, storage, thumbnail

log = get_logger(__name__)

GALLERY_DIR = "gallery"
INDEX_FILE = "index.html"
VIEW_FILE = "view.html"
_ASSET_DIR = "gallery_assets"

# One writer per process: the postprocess chain runs in a worker thread and
# replay-init in another, and two rebuilds racing on the same file would only
# waste work — the write itself is atomic, so the worst case is harmless, but
# there is no reason to do it twice at once.
_LOCK = threading.Lock()


class GalleryError(RuntimeError):
    """The gallery could not be produced."""


def gallery_dir(settings: Settings) -> Path:
    return settings.replay_dir / GALLERY_DIR


def index_path(settings: Settings) -> Path:
    return gallery_dir(settings) / INDEX_FILE


def view_path(settings: Settings) -> Path:
    return gallery_dir(settings) / VIEW_FILE


@dataclass(frozen=True, slots=True)
class _Card:
    """One site as the gallery shows it."""

    site_id: int
    title: str
    host: str
    folder: str
    tags: tuple[str, ...]
    captures: int
    when: str
    #: ISO of last_capture_at (or "" when never captured) — the "recent" sort key.
    sort_key: str
    on_feed: bool
    imported: bool
    #: The archived URL and timestamp this card opens, or None when there is
    #: nothing replayable to open.
    url: str | None
    timestamp: str | None
    #: A base64 `data:` URI of the homepage screenshot, or None for a placeholder.
    thumb: str | None


def write_gallery(session: Session, settings: Settings) -> Path:
    """Regenerate `index.html` and `view.html`, atomically. Returns the index.

    Best rebuilt in full every time, like the collection tree it lives beside:
    it is cheap at this scale and a rebuild can never drift from a partial
    update it never does.
    """
    with _LOCK:
        cards = _collect(session, settings)
        target = index_path(settings)
        try:
            storage.write_atomic(target, _render_index(cards, settings))
            storage.write_atomic(view_path(settings), _asset(VIEW_FILE))
        except OSError as exc:
            raise GalleryError(f"could not write the gallery: {exc}") from exc
        return target


# ── gathering ──────────────────────────────────────────────────────────────


def _collect(session: Session, settings: Settings) -> list[_Card]:
    sites = list(session.scalars(select(Site).where(Site.deleted_at.is_(None))).all())
    if not sites:
        return []
    ids = [s.id for s in sites]

    count_rows = session.execute(
        select(Capture.site_id, func.count())
        .where(Capture.site_id.in_(ids))
        .group_by(Capture.site_id)
    ).all()
    counts = {int(site_id): int(n) for site_id, n in count_rows}
    on_feed = set(
        session.scalars(
            select(Feed.site_id).where(Feed.enabled.is_(True), Feed.site_id.in_(ids)).distinct()
        ).all()
    )
    folders = {int(fid): str(path) for fid, path in session.execute(select(Folder.id, Folder.path))}
    tags = _tag_map(session, ids)

    now = utcnow()
    cards = [
        _card_for(
            settings,
            site,
            captures=counts.get(site.id, 0),
            tags=tags.get(site.id, ()),
            on_feed=site.id in on_feed,
            folder=folders.get(site.folder_id, ""),
            now=now,
        )
        for site in sites
    ]
    # Newest capture first — the server-rendered order, which the sort control
    # then lets the reader change. "" (never captured) sorts last under reverse.
    cards.sort(key=lambda c: c.sort_key, reverse=True)
    return cards


def _tag_map(session: Session, ids: list[int]) -> dict[int, tuple[str, ...]]:
    rows = session.execute(
        select(SiteTag.site_id, Tag.name)
        .join(Tag, Tag.id == SiteTag.tag_id)
        .where(SiteTag.site_id.in_(ids))
        .order_by(SiteTag.site_id, Tag.name)
    ).all()
    out: dict[int, list[str]] = {}
    for site_id, name in rows:
        out.setdefault(site_id, []).append(name)
    return {k: tuple(v) for k, v in out.items()}


def _card_for(
    settings: Settings,
    site: Site,
    *,
    captures: int,
    tags: tuple[str, ...],
    on_feed: bool,
    folder: str,
    now: datetime,
) -> _Card:
    from cairn.services import sites as site_service

    host = urlsplit(site.seed_url).hostname or site.primary_host or ""

    url: str | None = None
    timestamp: str | None = None
    thumb: str | None = None

    meta = thumbnail.describe(settings, site.archive_path)
    if meta is not None and thumbnail.exists(settings, site.archive_path):
        url = str(meta.get("url") or "") or None
        timestamp = str(meta.get("timestamp") or "") or None
        thumb = _data_uri(settings, site.archive_path)
    if url is None:
        # No screenshot (thumbnails off, or nothing photographable yet). It may
        # still have a page worth opening — find the newest one — but the card
        # gets a placeholder tile either way.
        record = thumbnail.subject(settings, site.archive_path, site_service.all_seeds(site))
        if record is not None:
            url, timestamp = record.url, record.timestamp

    imported = url is not None and urlsplit(url).path not in ("", "/")
    return _Card(
        site_id=site.id,
        title=site.title,
        host=host,
        folder=folder,
        tags=tags,
        captures=captures,
        when=_ago(site.last_capture_at, now),
        sort_key=to_iso(site.last_capture_at) if site.last_capture_at else "",
        on_feed=on_feed,
        imported=imported,
        url=url,
        timestamp=timestamp,
        thumb=thumb,
    )


def _data_uri(settings: Settings, archive_path: str) -> str | None:
    try:
        raw = thumbnail.image_path(settings, archive_path).read_bytes()
    except OSError:  # pragma: no cover — describe() said it was there a moment ago
        return None
    return f"data:{thumbnail.CONTENT_TYPE};base64," + base64.b64encode(raw).decode("ascii")


def _ago(dt: datetime | None, now: datetime) -> str:
    if dt is None:
        return "not captured"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    secs = max(0.0, (now - dt).total_seconds())
    mins, hours, days = secs / 60, secs / 3600, secs / 86400
    if secs < 90:
        return "just now"
    if mins < 90:
        return f"{round(mins)} min ago"
    if hours < 36:
        return f"{round(hours)} hr ago"
    if days < 14:
        return f"{round(days)} days ago"
    if days < 60:
        return f"{round(days / 7)} wk ago"
    if days < 545:
        return f"{round(days / 30)} mo ago"
    return f"{round(days / 365)} yr ago"


# ── rendering ────────────────────────────────────────────────────────────────


@functools.cache
def _asset(name: str) -> str:
    return files("cairn.services").joinpath(_ASSET_DIR).joinpath(name).read_text(encoding="utf-8")


def _render_index(cards: list[_Card], settings: Settings) -> str:
    template = _asset(INDEX_FILE)
    host_label = urlsplit(settings.replay_origin).hostname or "replay"
    if cards:
        grid = "\n".join(_card_html(c) for c in cards)
        body = (
            f'<div class="count" id="count">{_summary(cards)}</div>\n'
            f'<div class="grid" id="grid">\n{grid}\n</div>'
        )
    else:
        body = (
            '<div class="count" id="count"></div>\n'
            '<div class="grid" id="grid">'
            '<div class="empty">No archived sites yet. '
            "Capture one, and it appears here.</div></div>"
        )
    return template.replace("__HOST__", html.escape(host_label)).replace("__BODY__", body)


def _summary(cards: list[_Card]) -> str:
    n = len(cards)
    caps = sum(c.captures for c in cards)
    return (
        f"<b>{n}</b> {'site' if n == 1 else 'sites'} "
        f'<span class="dot">·</span> '
        f"<b>{caps}</b> {'capture' if caps == 1 else 'captures'} "
        f'<span class="dot">·</span> newest first'
    )


def _media(card: _Card) -> str:
    if card.thumb is not None:
        return (
            f'<img class="shot" src="{card.thumb}" '
            f'alt="Archived homepage of {html.escape(card.title)}" '
            f'width="640" height="400" loading="lazy">'
        )
    return f'<div class="ph-thumb">{_PLACEHOLDER_SVG}<span>no viewable capture</span></div>'


def _thumb_block(card: _Card) -> str:
    badges = '<span class="chip feed">on feed</span>' if card.on_feed else ""
    caps = f"{card.captures} {'capture' if card.captures == 1 else 'captures'}"
    return (
        f'<div class="thumb">{_media(card)}'
        f'<div class="badges">{badges}</div>'
        f'<div class="caps mono">{caps}</div></div>'
    )


def _card_html(card: _Card) -> str:
    title = html.escape(card.title)
    host = html.escape(card.host)
    terms = " ".join([card.title, card.host, card.folder, *card.tags]).lower()
    search = html.escape(terms, quote=True)
    title_attr = html.escape(card.title.lower(), quote=True)
    when = html.escape(card.when)

    if card.url is not None and card.timestamp is not None:
        coll = replay.collection_name(card.site_id)
        bare = f"/{coll}/{card.timestamp}mp_/{html.escape(card.url, quote=True)}"
        u = html.escape(quote(card.url, safe=""), quote=True)
        view = f"view.html?c={coll}&amp;t={card.timestamp}&amp;u={u}"
        kind = "imported" if card.imported else "homepage"
        meta = (
            f'<span class="mono">{when}</span><span class="sep">·</span>'
            f'<span>{kind}</span><span class="go">open &rsaquo;</span>'
        )
        return (
            f'<a class="card" href="{bare}" data-view="{view}" data-search="{search}" '
            f'data-sort="{card.sort_key}" data-title="{title_attr}">{_thumb_block(card)}'
            f'<div class="body"><div class="title">{title}</div>'
            f'<div class="domain mono">{host}</div>'
            f'<div class="meta">{meta}</div></div></a>'
        )

    meta = (
        f'<span class="mono">{when}</span><span class="sep">·</span><span>no capture to show</span>'
    )
    return (
        f'<div class="card ph" data-search="{search}" data-sort="{card.sort_key}" '
        f'data-title="{title_attr}">{_thumb_block(card)}'
        f'<div class="body"><div class="title">{title}</div>'
        f'<div class="domain mono">{host}</div>'
        f'<div class="meta">{meta}</div></div></div>'
    )


_PLACEHOLDER_SVG = (
    '<svg viewBox="0 0 48 48" fill="none" stroke="currentColor" stroke-width="2" '
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    '<rect x="7" y="9" width="34" height="30" rx="3"/>'
    '<circle cx="17" cy="19" r="3"/><path d="m10 34 9-9 6 6 7-7 6 6"/></svg>'
)
