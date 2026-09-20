"""The archive gallery — the static front page of the replay origin.

The generator's whole job is to be correct with the app switched off: static
cards that work without JavaScript, a screenshot that opens the page it is a
picture of, and a site with nothing to show that says so rather than vanishing.
So the tests read the generated HTML and assert those properties directly.
"""

from __future__ import annotations

import base64
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from cairn.config import Settings
from cairn.db.models import Capture, Feed
from cairn.services import gallery, postprocess, storage, thumbnail
from cairn.services import sites as site_service
from tests.conftest import XHR

JPEG = b"\xff\xd8\xff" + b"body-bytes"


def _site(db: Session, settings: Settings, *, seed: str, title: str, tags: list[str] | None = None):
    return site_service.create_site(db, settings, seed_url=seed, title=title, tags=tags)


def _capture(db: Session, site, dir_name: str = "20260101T000000Z-full-wget") -> Capture:
    capture = Capture(
        site_id=site.id, kind="full", engine_id="wget-warc", dir_name=dir_name, status="ok"
    )
    db.add(capture)
    db.flush()
    return capture


def _thumb(settings: Settings, site, *, url: str, ts: str, data: bytes = JPEG) -> None:
    """Write the screenshot and its home.json, as the thumbnail step would."""
    directory = thumbnail.thumb_dir(settings, site.archive_path)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / thumbnail.IMAGE_FILE).write_bytes(data)
    (directory / thumbnail.META_FILE).write_text(
        json.dumps({"url": url, "timestamp": ts, "taken_at": "2026-01-01T00:00:00+00:00"}),
        encoding="utf-8",
    )


def _index(settings: Settings, site, rows: list[tuple[str, str, str, str, str]]) -> None:
    lines = []
    for url, stamp, status, mime, capture_dir in rows:
        payload = {
            "url": url,
            "mime": mime,
            "status": status,
            "digest": "sha1:x",
            "filename": f"{storage.CAPTURES_DIR}/{capture_dir}/warc/part-00000.warc.gz",
            "offset": 0,
            "length": 100,
        }
        lines.append(f"{thumbnail.replay.surt_key(url)} {stamp} {json.dumps(payload)}\n")
    path = thumbnail.replay.index_path(settings, site.archive_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(sorted(lines)), encoding="utf-8")


def _read(settings: Settings) -> str:
    return gallery.index_path(settings).read_text(encoding="utf-8")


# ── the file itself ──────────────────────────────────────────────────────


def test_an_empty_archive_still_writes_a_page(db: Session, settings: Settings) -> None:
    index = gallery.write_gallery(db, settings)
    assert index.is_file()
    assert gallery.view_path(settings).is_file()
    assert "No archived sites yet" in _read(settings)


def test_the_viewer_is_written_and_carries_the_replay_sandbox(
    db: Session, settings: Settings
) -> None:
    gallery.write_gallery(db, settings)
    view = gallery.view_path(settings).read_text(encoding="utf-8")
    # The scripts-off toggle is the whole reason the mini-viewer exists.
    assert 'id="scripts"' in view
    assert "allow-scripts allow-same-origin allow-forms allow-popups" in view
    assert "allow-same-origin allow-forms allow-popups" in view
    assert "referrerpolicy" in view
    assert "no-referrer" in view


# ── a site with a picture ────────────────────────────────────────────────


def test_a_captured_site_is_a_linked_card_with_its_screenshot_inlined(
    db: Session, settings: Settings
) -> None:
    site = _site(db, settings, seed="https://blog.example.com/", title="Example Blog")
    _capture(db, site)
    _thumb(settings, site, url="https://blog.example.com/", ts="20260101000000")

    gallery.write_gallery(db, settings)
    html = _read(settings)

    coll = gallery.replay.collection_name(site.id)
    # A real link, with the bare mp_ URL as the no-JS default.
    assert f'href="/{coll}/20260101000000mp_/https://blog.example.com/"' in html
    # And the mini-viewer as the enhanced target.
    assert f'data-view="view.html?c={coll}&amp;t=20260101000000&amp;u=' in html
    # The screenshot bytes, inlined.
    assert "data:image/jpeg;base64," + base64.b64encode(JPEG).decode() in html
    assert "1 capture" in html
    assert "homepage" in html


def test_the_link_opens_the_capture_the_picture_is_of(db: Session, settings: Settings) -> None:
    """Screenshot and destination are the same capture: the card uses the
    home.json timestamp, even when a newer capture exists in the index."""
    site = _site(db, settings, seed="https://blog.example.com/", title="Example")
    _index(
        settings,
        site,
        [
            ("https://blog.example.com/", "20260101000000", "200", "text/html", "a"),
            ("https://blog.example.com/", "20260901000000", "200", "text/html", "b"),
        ],
    )
    _thumb(settings, site, url="https://blog.example.com/", ts="20260101000000")

    gallery.write_gallery(db, settings)
    html = _read(settings)
    assert "20260101000000mp_" in html
    assert "20260901000000mp_" not in html


def test_a_feed_earns_the_on_feed_chip(db: Session, settings: Settings) -> None:
    site = _site(db, settings, seed="https://blog.example.com/", title="Example")
    db.add(Feed(site_id=site.id, url="https://blog.example.com/feed", enabled=True))
    db.flush()
    _thumb(settings, site, url="https://blog.example.com/", ts="20260101000000")

    gallery.write_gallery(db, settings)
    assert "on feed" in _read(settings)


def test_an_imported_deep_page_is_labelled_imported(db: Session, settings: Settings) -> None:
    site = _site(db, settings, seed="https://notes.example.org/", title="Notes")
    _thumb(settings, site, url="https://notes.example.org/2019/03/post.html", ts="20260101000000")

    gallery.write_gallery(db, settings)
    assert "imported" in _read(settings)


# ── a site with nothing to show ──────────────────────────────────────────


def test_a_site_with_no_replayable_capture_gets_a_placeholder_not_a_link(
    db: Session, settings: Settings
) -> None:
    """The gated blog: a card that says so beats a card that is not there."""
    site = _site(db, settings, seed="https://gated.example.com/", title="Gated")
    _index(
        settings, site, [("https://gated.example.com/", "20260101000000", "302", "text/html", "a")]
    )

    gallery.write_gallery(db, settings)
    html = _read(settings)
    assert "no viewable capture" in html
    assert "Gated" in html
    # A placeholder tile, not a link, with nothing to open.
    assert '<div class="card ph"' in html
    assert '<a class="card"' not in html
    assert "mp_/https://gated.example.com" not in html


def test_a_page_without_a_screenshot_is_still_openable(db: Session, settings: Settings) -> None:
    """Thumbnails off, but a real page in the index: placeholder tile, live link."""
    site = _site(db, settings, seed="https://blog.example.com/", title="Example")
    _index(
        settings, site, [("https://blog.example.com/", "20260101000000", "200", "text/html", "a")]
    )

    gallery.write_gallery(db, settings)
    html = _read(settings)
    assert "no viewable capture" in html  # placeholder image
    assert "20260101000000mp_/https://blog.example.com/" in html  # but linked


# ── safety ───────────────────────────────────────────────────────────────


def test_a_title_with_markup_is_escaped(db: Session, settings: Settings) -> None:
    site = _site(db, settings, seed="https://x.example.com/", title='Ada & <b>Co</b> "1"')
    _thumb(settings, site, url="https://x.example.com/", ts="20260101000000")

    gallery.write_gallery(db, settings)
    html = _read(settings)
    assert "<b>Co</b>" not in html
    assert "Ada &amp; &lt;b&gt;Co&lt;/b&gt;" in html


# ── the per-capture hook ─────────────────────────────────────────────────


def _ctx(db: Session, settings: Settings, site, capture: Capture) -> postprocess.Context:
    return postprocess.Context(
        session=db,
        settings=settings,
        capture=capture,
        site=site,
        output_dir=storage.site_dir(settings, site.archive_path),
        tool_version=None,
        stats={},
        scope={},
        seeds=[site.seed_url],
        seed_source={},
        artifacts=[],
        warnings=[],
    )


def test_the_capture_step_rebuilds_the_gallery(db: Session, settings: Settings) -> None:
    site = _site(db, settings, seed="https://blog.example.com/", title="Example")
    capture = _capture(db, site)
    _thumb(settings, site, url="https://blog.example.com/", ts="20260101000000")

    ctx = _ctx(db, settings, site, capture)
    postprocess.step_gallery(ctx)

    assert gallery.index_path(settings).is_file()
    assert "Example" in _read(settings)
    assert "gallery_skipped" not in ctx.stats


def test_a_gallery_failure_never_downgrades_a_capture(
    db: Session, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    site = _site(db, settings, seed="https://blog.example.com/", title="Example")
    capture = _capture(db, site)

    def boom(*_a: object, **_k: object) -> object:
        raise gallery.GalleryError("disk full")

    monkeypatch.setattr(gallery, "write_gallery", boom)
    ctx = _ctx(db, settings, site, capture)
    postprocess.step_gallery(ctx)

    assert ctx.stats["gallery_skipped"] == "disk full"
    assert ctx.warnings == []
    assert capture.status == "ok"


# ── deletion clears the card ──────────────────────────────────────────────


def test_deleting_a_site_takes_it_out_of_the_gallery(
    authed: TestClient, db: Session, settings: Settings
) -> None:
    site = _site(db, settings, seed="https://blog.example.com/", title="Doomed")
    _thumb(settings, site, url="https://blog.example.com/", ts="20260101000000")
    db.commit()

    gallery.write_gallery(db, settings)
    assert "Doomed" in _read(settings)

    resp = authed.delete(f"/api/sites/{site.id}", headers=XHR)
    assert resp.status_code in (200, 204), resp.text
    assert "Doomed" not in _read(settings)


def test_rebuild_collections_writes_the_gallery(
    authed: TestClient, db: Session, settings: Settings
) -> None:
    site = _site(db, settings, seed="https://blog.example.com/", title="Example")
    _thumb(settings, site, url="https://blog.example.com/", ts="20260101000000")
    db.commit()

    resp = authed.post("/api/maintenance/rebuild-collections", headers=XHR)
    assert resp.status_code == 200, resp.text
    assert gallery.index_path(settings).is_file()
    assert "Example" in _read(settings)
