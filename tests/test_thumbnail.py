"""The picture on the site card.

The interesting half of this feature is not taking a screenshot, it is
deciding *what* and *whether*. Photographing the live site is one line and
gives a card that lies about a blog that has closed; photographing a URL that
is not in the archive gives a card showing pywb's error page; photographing
after every incremental capture starts a browser per new post.

So most of what follows is about the "no" cases, and the one end-to-end test
asserts the thing all of it exists for: the picture is of the archive, taken
with the original site switched off.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from cairn.config import Settings
from cairn.db.models import Capture, Site
from cairn.services import postprocess, replay, settings_store, storage, thumbnail
from cairn.services import sites as site_service
from tests.conftest import XHR

JPEG_MAGIC = b"\xff\xd8\xff"

HOME = "https://blog.example.com/"
HTML = "text/html"
FULL = "20260101T000000Z-full-wget"
LATER = "20260601T000000Z-full-wget"
FEED = "20260602T000000Z-feed-wget"


def _site(db: Session, settings: Settings, seed: str = "https://blog.example.com/") -> Site:
    return site_service.create_site(db, settings, seed_url=seed)


def _capture(db: Session, site: Site, dir_name: str = "20260101T000000Z-full-wget") -> Capture:
    capture = Capture(
        site_id=site.id, kind="full", engine_id="wget-warc", dir_name=dir_name, status="ok"
    )
    db.add(capture)
    db.flush()
    return capture


def _index(settings: Settings, site: Site, rows: list[tuple[str, str, str, str, str]]) -> None:
    """Write a CDXJ by hand: (url, timestamp, status, mime, capture dir)."""
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
        lines.append(f"{replay.surt_key(url)} {stamp} {json.dumps(payload)}\n")
    path = replay.index_path(settings, site.archive_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(sorted(lines)), encoding="utf-8")


def _write_thumbnail(settings: Settings, site: Site, data: bytes = b"old") -> Path:
    path = thumbnail.image_path(settings, site.archive_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


# ── what gets photographed ───────────────────────────────────────────────


def test_the_newest_archived_version_of_the_homepage_is_chosen(
    db: Session, settings: Settings
) -> None:
    site = _site(db, settings)
    _index(
        settings,
        site,
        [
            ("https://blog.example.com/", "20260101000000", "200", "text/html", "a"),
            ("https://blog.example.com/", "20260601000000", "200", "text/html", "b"),
            ("https://blog.example.com/post.html", "20260901000000", "200", "text/html", "b"),
        ],
    )
    chosen = thumbnail.subject(settings, site.archive_path, [site.seed_url])
    assert chosen is not None
    assert chosen.url == "https://blog.example.com/"
    assert chosen.timestamp == "20260601000000"


def test_a_redirect_is_not_a_page_and_neither_is_a_404(db: Session, settings: Settings) -> None:
    """The gated-blog archive: one 302 to a content warning, nothing else.

    Photographing it would put pywb's "URL Not Found" on the card of exactly
    the site that most needs somebody to look at it.
    """
    site = _site(db, settings)
    _index(
        settings,
        site,
        [
            ("https://blog.example.com/", "20260101000000", "302", "text/html", "a"),
            ("https://blog.example.com/gone", "20260101000000", "404", "text/html", "a"),
        ],
    )
    assert thumbnail.subject(settings, site.archive_path, [site.seed_url]) is None


def test_a_site_whose_homepage_was_never_captured_still_gets_a_picture(
    db: Session, settings: Settings
) -> None:
    """The URL importer's shape: seeded at the origin, only pages archived.

    Insisting on the seed would leave every imported bookmark list blank.
    """
    site = _site(db, settings, seed="https://notes.example.org/")
    _index(
        settings,
        site,
        [
            (
                "https://notes.example.org/2019/03/post.html",
                "20260101000000",
                "200",
                "text/html",
                "a",
            ),
        ],
    )
    chosen = thumbnail.subject(settings, site.archive_path, [site.seed_url])
    assert chosen is not None
    assert chosen.url.endswith("post.html")


def test_a_second_seed_is_tried_before_falling_back(db: Session, settings: Settings) -> None:
    """A site that moved domain: the old address is archived, the new one is not."""
    site = _site(db, settings, seed="https://new.example.com/")
    site_service.add_seed(db, settings, site, "https://old.example.net/")
    _index(
        settings,
        site,
        [
            ("https://old.example.net/", "20260101000000", "200", "text/html", "a"),
            ("https://old.example.net/z-post.html", "20260101000000", "200", "text/html", "a"),
        ],
    )
    chosen = thumbnail.subject(settings, site.archive_path, site_service.all_seeds(site))
    assert chosen is not None
    assert chosen.url == "https://old.example.net/"


def test_a_record_is_matched_to_the_capture_that_wrote_it(db: Session, settings: Settings) -> None:
    site = _site(db, settings)
    _index(settings, site, [(HOME, "20260101000000", "200", HTML, "aa")])
    record = thumbnail.subject(settings, site.archive_path, [site.seed_url])
    assert record is not None
    assert thumbnail.is_from_capture(record, "aa")
    # Not a prefix match on the directory name: "aa" must not match "aaa".
    assert not thumbnail.is_from_capture(record, "a")
    assert not thumbnail.is_from_capture(record, "bb")


# ── whether it is taken at all ───────────────────────────────────────────


class _Recorder:
    """Stands in for the browser, so these tests are about the decision."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, settings: Settings, **kwargs: Any) -> thumbnail.Shot:
        self.calls.append(kwargs)
        record = kwargs.get("record")
        thumbnail.image_path(settings, kwargs["archive_path"]).parent.mkdir(
            parents=True, exist_ok=True
        )
        thumbnail.image_path(settings, kwargs["archive_path"]).write_bytes(b"jpeg")
        return thumbnail.Shot(
            url=record.url if record else "", timestamp=record.timestamp if record else "", bytes=4
        )


def _run_step(db: Session, settings: Settings, site: Site, capture: Capture) -> postprocess.Context:
    ctx = postprocess.Context(
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
    postprocess.step_thumbnail(ctx)
    return ctx


def test_a_capture_that_refreshed_the_page_takes_a_new_picture(
    db: Session, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    site = _site(db, settings)
    capture = _capture(db, site, LATER)
    _index(
        settings,
        site,
        [
            (
                "https://blog.example.com/",
                "20260101000000",
                "200",
                "text/html",
                "20260101T000000Z-full-wget",
            ),
            (
                "https://blog.example.com/",
                "20260601000000",
                "200",
                "text/html",
                "20260601T000000Z-full-wget",
            ),
        ],
    )
    _write_thumbnail(settings, site)
    recorder = _Recorder()
    monkeypatch.setattr(thumbnail, "capture_site", recorder)

    ctx = _run_step(db, settings, site, capture)
    assert len(recorder.calls) == 1
    assert ctx.stats["thumbnail"]["timestamp"] == "20260601000000"


def test_an_incremental_capture_of_one_post_does_not_start_a_browser(
    db: Session, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rule that decides whether this feature is cheap or expensive.

    A feed poll captures one new post and leaves the front page alone. Taking
    the picture again would launch Chromium to produce the same image, once per
    post, forever.
    """
    site = _site(db, settings)
    capture = _capture(db, site, FEED)
    _index(
        settings,
        site,
        [
            (
                "https://blog.example.com/",
                "20260101000000",
                "200",
                "text/html",
                "20260101T000000Z-full-wget",
            ),
            (
                "https://blog.example.com/new.html",
                "20260602000000",
                "200",
                "text/html",
                "20260602T000000Z-feed-wget",
            ),
        ],
    )
    _write_thumbnail(settings, site)
    recorder = _Recorder()
    monkeypatch.setattr(thumbnail, "capture_site", recorder)

    _run_step(db, settings, site, capture)
    assert recorder.calls == []


def test_a_site_with_no_picture_yet_gets_one_from_any_capture(
    db: Session, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of the rule, or a site captured before this existed
    would stay blank until somebody re-captured the whole thing."""
    site = _site(db, settings)
    capture = _capture(db, site, FEED)
    _index(
        settings,
        site,
        [
            (
                "https://blog.example.com/",
                "20260101000000",
                "200",
                "text/html",
                "20260101T000000Z-full-wget",
            ),
        ],
    )
    recorder = _Recorder()
    monkeypatch.setattr(thumbnail, "capture_site", recorder)

    _run_step(db, settings, site, capture)
    assert len(recorder.calls) == 1


def test_the_setting_switches_it_off(
    db: Session, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    site = _site(db, settings)
    capture = _capture(db, site)
    _index(settings, site, [(HOME, "20260101000000", "200", HTML, "x")])
    settings_store.put(db, thumbnail.ENABLED_SETTING, False)
    recorder = _Recorder()
    monkeypatch.setattr(thumbnail, "capture_site", recorder)

    _run_step(db, settings, site, capture)
    assert recorder.calls == []


def test_a_failure_is_recorded_and_never_warned_about(
    db: Session, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A warning per capture per site would train the reader to skip them all.

    The media step warns because somebody switched media on for that site. A
    thumbnail is decoration: an instance whose replay sidecar is not running
    would otherwise carry a warning on every capture it ever makes.
    """
    site = _site(db, settings)
    capture = _capture(db, site)
    _index(settings, site, [(HOME, "20260101000000", "200", HTML, "x")])

    def refuse(*_args: Any, **_kwargs: Any) -> thumbnail.Shot:
        raise thumbnail.ThumbnailError("replay is not answering on http://127.0.0.1:8081")

    monkeypatch.setattr(thumbnail, "capture_site", refuse)
    ctx = _run_step(db, settings, site, capture)

    assert ctx.warnings == []
    assert "replay is not answering" in ctx.stats["thumbnail_skipped"]
    assert capture.status == "ok"


def test_an_archive_with_nothing_to_show_is_not_an_error(
    db: Session, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    site = _site(db, settings)
    capture = _capture(db, site)
    _index(settings, site, [(HOME, "20260101000000", "302", HTML, "x")])
    recorder = _Recorder()
    monkeypatch.setattr(thumbnail, "capture_site", recorder)

    ctx = _run_step(db, settings, site, capture)
    assert recorder.calls == []
    assert "thumbnail_skipped" not in ctx.stats
    assert ctx.warnings == []


# ── serving it ───────────────────────────────────────────────────────────


def test_the_card_reflects_the_file_and_not_a_column(
    authed: TestClient, db: Session, settings: Settings
) -> None:
    """Deleting the image is enough to clear the flag.

    A boolean the database owned would keep claiming a picture that a folder
    move, a restore or a hand-deleted directory had taken away — and the card
    would show a broken image with nothing to press to fix it.
    """
    site = _site(db, settings)
    db.commit()
    assert authed.get("/api/sites", headers=XHR).json()["items"][0]["has_thumbnail"] is False

    path = _write_thumbnail(settings, site, JPEG_MAGIC + b"body")
    assert authed.get("/api/sites", headers=XHR).json()["items"][0]["has_thumbnail"] is True

    path.unlink()
    assert authed.get("/api/sites", headers=XHR).json()["items"][0]["has_thumbnail"] is False


def test_the_image_is_served_with_a_fixed_type_and_revalidates(
    authed: TestClient, db: Session, settings: Settings
) -> None:
    site = _site(db, settings)
    db.commit()
    assert authed.get(f"/api/sites/{site.id}/thumbnail", headers=XHR).status_code == 404

    _write_thumbnail(settings, site, JPEG_MAGIC + b"body")
    response = authed.get(f"/api/sites/{site.id}/thumbnail", headers=XHR)
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.content.startswith(JPEG_MAGIC)

    etag = response.headers["etag"]
    again = authed.get(f"/api/sites/{site.id}/thumbnail", headers={**XHR, "If-None-Match": etag})
    assert again.status_code == 304
    assert again.content == b""


def test_the_thumbnail_needs_a_session(client: TestClient, db: Session, settings: Settings) -> None:
    site = _site(db, settings)
    db.commit()
    _write_thumbnail(settings, site, JPEG_MAGIC)
    assert client.get(f"/api/sites/{site.id}/thumbnail").status_code == 401


def test_the_setting_round_trips(authed: TestClient) -> None:
    assert authed.get("/api/thumbnails/settings", headers=XHR).json()["enabled"] is True
    put = authed.put("/api/thumbnails/settings", json={"enabled": False}, headers=XHR)
    assert put.status_code == 200
    assert authed.get("/api/thumbnails/settings", headers=XHR).json()["enabled"] is False


def test_the_backfill_is_a_job(authed: TestClient) -> None:
    started = authed.post("/api/maintenance/thumbnails", headers=XHR)
    assert started.status_code == 202, started.text
    assert started.json()["job_id"]


# ── the whole thing, for real ────────────────────────────────────────────
#
# A real wget crawl, a real pywb and a real Chromium. Needs all three, so it
# runs in the container and in CI.

STARTUP_TIMEOUT_S = 30


class _Stoppable:
    """A fixture site that the test can switch off partway through."""

    def __init__(self, server: Any) -> None:
        self._server = server
        self.url = f"http://127.0.0.1:{server.server_address[1]}/"

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def closing_site() -> Iterator[_Stoppable]:
    import threading
    from http.server import ThreadingHTTPServer

    from tests.conftest import _Handler  # the same pages the other e2e tests crawl

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    site = _Stoppable(server)
    try:
        yield site
    finally:
        with contextlib.suppress(Exception):
            site.stop()


def _free_port() -> int:
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture
def pywb(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """A real pywb, on a port the code under test is told to look at.

    Unlike the other replay suites this one cannot simply pick a port and build
    its own URLs: the code under test asks `settings.replay_port` where replay
    is. Binding the configured default instead would be a trap, and it caught
    me — running this suite inside the *shipped* image starts s6, which starts
    pywb on that very port over a different collections tree. Our `wayback`
    then loses the bind while the port still answers, the test happily
    photographs somebody else's 404 page, and it passes.

    So the port is chosen here and `settings` is pointed at it. Nothing else
    can be holding it, and `replay_internal_origin` picks it up because that is
    the one place the port is read.
    """
    port = _free_port()
    monkeypatch.setattr(settings, "replay_port", port)
    replay.write_config(settings)
    proc = subprocess.Popen(
        ["wayback", "--port", str(port), "--bind", "127.0.0.1"],
        cwd=str(settings.replay_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    base = settings.replay_internal_origin
    try:
        deadline = time.monotonic() + STARTUP_TIMEOUT_S
        while time.monotonic() < deadline:
            if proc.poll() is not None:  # pragma: no cover — pywb died on startup
                raise AssertionError(f"pywb exited: {proc.communicate(timeout=10)[0]}")
            try:
                urllib.request.urlopen(f"{base}/", timeout=2)
                break
            except OSError:
                time.sleep(0.3)
        else:  # pragma: no cover — pywb failed to start
            proc.terminate()
            raise AssertionError(f"pywb did not start:\n{proc.communicate(timeout=10)[0]}")
        yield base
    finally:
        proc.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.communicate(timeout=15)


@pytest.mark.skipif(shutil.which("wget") is None, reason="needs GNU wget on PATH")
@pytest.mark.skipif(shutil.which("wayback") is None, reason="needs pywb on PATH")
@pytest.mark.skipif(os.name == "nt", reason="mingw wget hits MAX_PATH; runs in Docker/CI")
def test_the_picture_is_of_the_archive_with_the_original_switched_off(
    authed: TestClient,
    settings: Settings,
    closing_site: _Stoppable,
    pywb: str,
) -> None:
    """The whole reason this photographs replay rather than the live URL.

    The fixture site is captured, then **switched off**, and only then is the
    picture taken. Pointed at the live address this would fail outright — which
    is the state every archived site eventually reaches, and the one where a
    thumbnail of the live web would quietly start lying.
    """
    from cairn.services import browser

    ok, why = browser.availability()
    if not ok:
        pytest.skip(why)

    from tests.test_capture_e2e import run_capture

    _job, site_id = run_capture(authed, closing_site.url)
    detail = authed.get(f"/api/sites/{site_id}", headers=XHR).json()

    closing_site.stop()
    assert _is_closed(closing_site.url), "the fixture site is still answering"

    # The capture's own post-processor already took one, while the fixture was
    # still up. Removed, so what is asserted below is unambiguously the picture
    # taken after the original went away.
    existing = thumbnail.image_path(settings, detail["archive_path"])
    assert existing.is_file(), "the capture's screenshot step did not run"
    existing.unlink()

    # Prove replay is serving *this* archive before photographing it. Without
    # this the test is happy to assert on a picture of pywb's "URL Not Found",
    # which is a valid JPEG of the right size and says nothing — and that is
    # not hypothetical, it is what this test did until the fixture stopped
    # sharing a port with the container's own pywb.
    record = thumbnail.subject(settings, detail["archive_path"], [detail["seed_url"]])
    assert record is not None
    bare = f"{pywb}/{replay.collection_name(site_id)}/{record.timestamp}mp_/{record.url}"
    with urllib.request.urlopen(bare, timeout=10) as response:
        assert response.status == 200
        assert b"<h1>Index</h1>" in response.read(), "replay is not serving this archive"

    shot = thumbnail.capture_site(
        settings,
        site_id=site_id,
        archive_path=detail["archive_path"],
        seeds=[detail["seed_url"]],
    )
    assert shot.url.startswith(closing_site.url)
    assert shot.bytes > 1_000

    described = thumbnail.describe(settings, detail["archive_path"])
    assert described is not None
    assert described["url"] == shot.url

    listed = authed.get("/api/sites", headers=XHR).json()["items"]
    assert listed[0]["has_thumbnail"] is True
    served = authed.get(f"/api/sites/{site_id}/thumbnail", headers=XHR)
    assert served.status_code == 200
    assert served.content.startswith(JPEG_MAGIC), "not a JPEG"
    assert len(served.content) > 1_000, "suspiciously small for a rendered page"


def _is_closed(url: str) -> bool:
    try:
        urllib.request.urlopen(url, timeout=2)
    except OSError:
        return True
    return False
