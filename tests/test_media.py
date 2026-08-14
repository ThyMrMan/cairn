"""Finding and downloading the media an archived post embedded.

Every fetch here is against a fixture on this machine. yt-dlp will happily go
to YouTube if you hand it a YouTube URL — the probe that established what this
can do without ffmpeg did exactly that once, by following an embed in a test
page — so the tests are written so that cannot happen: the only URLs offered
to the downloader are the fixture's own.
"""

from __future__ import annotations

import json
import shutil
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from cairn.db.models import Capture, Site
from cairn.db.types import utcnow
from cairn.services import media, storage
from tests.conftest import XHR

# ftyp + a body. Structurally enough of an MP4 for yt-dlp's generic extractor,
# and nothing has to decode it.
MP4 = bytes.fromhex("0000001c667479706d703432000002006d70343169736f6d61766331") + b"\x00" * 8192


@pytest.fixture
def media_server() -> Iterator[str]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:
            body = MP4 * (200 if self.path.startswith("/big") else 1)
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            self.wfile.write(body)

        def do_HEAD(self) -> None:
            self.do_GET()

        def log_message(self, *_args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


# ── finding it ───────────────────────────────────────────────────────────


def test_a_video_tag_is_found() -> None:
    html = b"""<html><body><div class='post-body'>
    <video controls src="/clip.mp4"></video></div></body></html>"""
    assert media.find_embeds(html, "http://blog.test/post.html") == ["http://blog.test/clip.mp4"]


def test_source_elements_are_found() -> None:
    html = b"""<video><source src="/a.webm" type="video/webm">
    <source src="/a.mp4" type="video/mp4"></video>"""
    found = media.find_embeds(html, "http://blog.test/")
    assert found == ["http://blog.test/a.webm", "http://blog.test/a.mp4"]


def test_a_youtube_embed_is_found() -> None:
    html = b'<iframe src="https://www.youtube.com/embed/abc123" width="560"></iframe>'
    assert media.find_embeds(html, "http://blog.test/") == ["https://www.youtube.com/embed/abc123"]


def test_an_iframe_that_is_not_a_video_is_left_alone() -> None:
    """Every page has iframes. Handing all of them to a downloader is how a
    capture ends up fetching from thirty hosts nobody meant to involve."""
    html = b"""
    <iframe src="https://disqus.com/embed/comments"></iframe>
    <iframe src="https://www.google.com/maps/embed?pb=x"></iframe>
    <iframe src="https://adserver.example/ad?id=1"></iframe>
    <iframe src="https://player.vimeo.com/video/76979871"></iframe>
    """
    assert media.find_embeds(html, "http://blog.test/") == [
        "https://player.vimeo.com/video/76979871"
    ]


def test_a_link_to_a_page_is_not_media() -> None:
    html = b'<a href="/post.html">a post</a><img src="/photo.jpg">'
    assert media.find_embeds(html, "http://blog.test/") == []


def test_the_same_url_twice_is_one_item() -> None:
    html = b"""<video src="/clip.mp4"></video><video src="/clip.mp4"></video>"""
    assert media.find_embeds(html, "http://blog.test/") == ["http://blog.test/clip.mp4"]


# ── the guard ────────────────────────────────────────────────────────────


def test_a_loopback_url_is_refused() -> None:
    """These URLs come out of archived HTML somebody else wrote — the one
    genuinely attacker-controlled fetch target in this application."""
    assert media.check_url("http://127.0.0.1:8080/clip.mp4", allow_private=False)
    assert media.check_url("http://localhost/clip.mp4", allow_private=False)


def test_private_ranges_are_refused() -> None:
    for host in ("10.0.0.1", "192.168.1.1", "172.16.0.5", "169.254.169.254"):
        reason = media.check_url(f"http://{host}/clip.mp4", allow_private=False)
        assert reason, f"{host} was allowed"
        assert "public" in reason


def test_a_scheme_that_is_not_http_is_refused() -> None:
    assert media.check_url("file:///etc/passwd", allow_private=False)
    assert media.check_url("gopher://example.com/x", allow_private=False)


def test_the_guard_can_be_turned_off_deliberately() -> None:
    assert media.check_url("http://127.0.0.1:8080/clip.mp4", allow_private=True) == ""


def test_a_name_that_does_not_resolve_is_refused() -> None:
    reason = media.check_url("https://no-such-host.invalid/clip.mp4", allow_private=False)
    assert "does not resolve" in reason


# ── downloading ──────────────────────────────────────────────────────────


needs_ytdlp = pytest.mark.skipif(not media.available()[0], reason="needs yt-dlp")


@needs_ytdlp
def test_a_direct_file_is_downloaded(media_server: str, tmp_path: Path) -> None:
    policy = {**media.DEFAULT_POLICY, "allow_private_hosts": True}
    result = media.download([f"{media_server}/clip.mp4"], tmp_path, policy)

    assert result.downloaded == 1, [i.to_dict() for i in result.items]
    item = result.items[0]
    assert item.status == "downloaded"
    assert (tmp_path / item.filename).is_file()
    assert item.bytes == len(MP4)


@needs_ytdlp
def test_the_guard_stops_the_download_by_default(media_server: str, tmp_path: Path) -> None:
    """The same URL, with the policy left alone. Nothing is fetched, and the
    refusal says why rather than vanishing."""
    result = media.download([f"{media_server}/clip.mp4"], tmp_path, media.DEFAULT_POLICY)

    assert result.downloaded == 0
    assert result.items[0].status == "skipped"
    assert "public address" in result.items[0].reason
    assert not list(tmp_path.iterdir())


@needs_ytdlp
def test_an_item_over_the_size_limit_is_not_kept(media_server: str, tmp_path: Path) -> None:
    policy = {
        **media.DEFAULT_POLICY,
        "allow_private_hosts": True,
        "max_item_bytes": 4096,
    }
    result = media.download([f"{media_server}/big.mp4"], tmp_path, policy)

    assert result.downloaded == 0
    assert result.items[0].status in ("skipped", "failed")
    assert not any(p.suffix == ".mp4" for p in tmp_path.iterdir())


@needs_ytdlp
def test_the_item_count_is_capped(media_server: str, tmp_path: Path) -> None:
    policy = {**media.DEFAULT_POLICY, "allow_private_hosts": True, "max_items": 2}
    urls = [f"{media_server}/clip{n}.mp4" for n in range(5)]
    result = media.download(urls, tmp_path, policy)

    assert result.downloaded == 2
    assert result.skipped == 3
    assert all("limit of 2" in i.reason for i in result.items if i.status == "skipped")


@needs_ytdlp
def test_the_total_budget_is_respected(media_server: str, tmp_path: Path) -> None:
    policy = {
        **media.DEFAULT_POLICY,
        "allow_private_hosts": True,
        "max_total_bytes": len(MP4),
    }
    urls = [f"{media_server}/clip{n}.mp4" for n in range(3)]
    result = media.download(urls, tmp_path, policy)

    assert result.downloaded == 1
    assert any("budget is spent" in i.reason for i in result.items)


@needs_ytdlp
def test_a_dead_url_is_reported_not_raised(tmp_path: Path) -> None:
    policy = {**media.DEFAULT_POLICY, "allow_private_hosts": True}
    result = media.download(["http://127.0.0.1:1/nothing.mp4"], tmp_path, policy)

    assert result.failed == 1
    assert result.items[0].status == "failed"
    assert result.items[0].reason


def test_without_yt_dlp_every_item_says_so(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(media, "available", lambda: (False, "yt-dlp is not installed"))
    result = media.download(["https://example.com/a.mp4"], tmp_path, media.DEFAULT_POLICY)
    assert result.failed == 1
    assert "yt-dlp" in result.items[0].reason


def test_ffmpeg_is_deliberately_absent() -> None:
    """The default format asks for a single file. If ffmpeg ever arrives in
    the image this is the test that should make somebody reconsider the
    default, rather than it changing silently."""
    assert shutil.which("ffmpeg") is None
    assert "best[ext=mp4]" in media.DEFAULT_POLICY["format"]


# ── through a real capture ───────────────────────────────────────────────


@pytest.mark.skipif(shutil.which("wget") is None, reason="needs GNU wget on PATH")
@needs_ytdlp
def test_a_capture_of_a_page_with_a_video_gets_the_video(authed, settings, tmp_path: Path) -> None:
    """The whole point, end to end: wget captures the page and cannot capture
    the video, and the media step goes back for it."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from cairn.services import storage
    from tests.conftest import XHR

    page = (
        b"<!DOCTYPE html><html><head><title>A post with a clip</title></head><body>"
        b"<div class='post-body entry-content'><p>Here is the clip.</p>"
        b'<video controls src="/clip.mp4"></video></div></body></html>'
    )

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:
            if self.path == "/robots.txt":
                body, ctype = b"User-agent: *\nAllow: /\n", "text/plain"
            elif self.path.startswith("/clip.mp4"):
                body, ctype = MP4, "video/mp4"
            else:
                body, ctype = page, "text/html"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            self.wfile.write(body)

        def do_HEAD(self) -> None:
            self.do_GET()

        def log_message(self, *_args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}/"
    try:
        created = authed.post("/api/sites", json={"seed_url": base, "title": "Clips"}, headers=XHR)
        site_id = created.json()["id"]

        # On for this site, and the private-host guard lifted because the
        # fixture is on loopback — which is exactly the pair the guard exists
        # to keep separate.
        #
        # Through the endpoint, not by writing `scope_settings` directly. This
        # test used to reach into the session and set it, which is what a test
        # does when the feature has no way in — and is why the missing endpoint
        # went unnoticed: the only thing exercising the setting was a test that
        # had bypassed it.
        saved = authed.put(
            f"/api/sites/{site_id}/media",
            json={"enabled": True, "allow_private_hosts": True},
            headers=XHR,
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["policy"]["enabled"] is True

        started = authed.post(f"/api/sites/{site_id}/capture", json={"kind": "full"}, headers=XHR)
        from tests.test_capture_e2e import wait_for_job

        job = wait_for_job(authed, started.json()["job_id"])
        assert job["status"] == "ok", job
    finally:
        server.shutdown()
        server.server_close()

    capture = authed.get(f"/api/sites/{site_id}/captures").json()[0]
    site = authed.get(f"/api/sites/{site_id}").json()
    downloaded = sorted(
        (
            storage.site_dir(settings, site["archive_path"])
            / storage.DERIVED_DIR
            / "media"
            / capture["dir_name"]
        ).iterdir()
    )
    assert downloaded, "the media step downloaded nothing"
    assert downloaded[0].stat().st_size == len(MP4)

    # The manifest records it too, which is what makes "the video is not here"
    # answerable years later without the database.
    detail = authed.get(f"/api/captures/{capture['id']}").json()
    assert detail["manifest"]["stats"]["media"]["downloaded"] == 1


# ── the policy, and reaching it ──────────────────────────────────────────
#
# The download step shipped in M9 and nothing could switch it on: the policy
# lives in `scope_settings["media"]` and no endpoint wrote it, so enabling the
# feature meant editing the database by hand. These cover the endpoints that
# closed that gap. None of them need yt-dlp — setting a policy, listing what a
# capture recorded, and serving a file are all reachable without it, which is
# also why the gap was invisible for so long.


def _site_with_media(db, settings, *, items: list[dict] | None = None) -> tuple[int, str, str]:
    """A site, one capture, and a manifest recording `items` as its media."""
    site = Site(
        folder_id=1,
        slug="clips",
        title="Clips",
        seed_url="http://blog.test/",
        primary_host="blog.test",
        archive_path="Unfiled/clips",
    )
    db.add(site)
    db.flush()
    storage.ensure_site_dirs(settings, site.archive_path)

    capture = Capture(
        site_id=site.id,
        kind="full",
        engine_id="wget-warc",
        dir_name="20260814-120000",
        status="ok",
        started_at=utcnow(),
    )
    db.add(capture)
    db.flush()
    storage.ensure_capture_dirs(settings, site.archive_path, capture.dir_name)

    block = {"found": len(items or []), "items": items or []}
    manifest = storage.manifest_path(settings, site.archive_path, capture.dir_name)
    manifest.write_text(json.dumps({"stats": {"media": block}}), encoding="utf-8")
    db.commit()
    return site.id, site.archive_path, capture.dir_name


def test_media_is_off_until_a_site_asks(authed, db, settings) -> None:
    site_id, _, _ = _site_with_media(db, settings)
    body = authed.get(f"/api/sites/{site_id}/media").json()
    assert body["policy"]["enabled"] is False
    assert body["override"] == {}
    assert body["items"] == []


def test_a_site_override_wins_over_the_instance_default(authed, db, settings) -> None:
    """Built-in under instance setting under site override, and the endpoint
    reports the merged result rather than any one layer."""
    from cairn.services import settings_store

    site_id, _, _ = _site_with_media(db, settings)
    settings_store.put(db, media.SETTING, {"enabled": True, "max_items": 5})
    db.commit()

    inherited = authed.get(f"/api/sites/{site_id}/media").json()["policy"]
    assert inherited["enabled"] is True
    assert inherited["max_items"] == 5

    saved = authed.put(f"/api/sites/{site_id}/media", json={"max_items": 2}, headers=XHR).json()
    assert saved["policy"]["max_items"] == 2
    # Still on, from the instance layer the override said nothing about.
    assert saved["policy"]["enabled"] is True
    # And the untouched built-in is still underneath both.
    assert saved["policy"]["format"] == media.DEFAULT_POLICY["format"]


def test_an_empty_body_returns_the_site_to_inheriting(authed, db, settings) -> None:
    site_id, _, _ = _site_with_media(db, settings)
    authed.put(f"/api/sites/{site_id}/media", json={"enabled": True}, headers=XHR)
    assert authed.get(f"/api/sites/{site_id}/media").json()["override"] == {"enabled": True}

    cleared = authed.put(f"/api/sites/{site_id}/media", json={}, headers=XHR).json()
    assert cleared["override"] == {}
    assert cleared["policy"]["enabled"] is False


def test_the_limits_are_enforced_on_the_server(authed, db, settings) -> None:
    """The form applies these too; a request that skips the form must not be
    able to set an unbounded per-capture budget."""
    site_id, _, _ = _site_with_media(db, settings)
    for bad in ({"max_items": -1}, {"max_items": 100_000}, {"max_total_bytes": -5}, {"format": ""}):
        response = authed.put(f"/api/sites/{site_id}/media", json=bad, headers=XHR)
        assert response.status_code == 422, (bad, response.text)


def test_the_listing_reports_refusals_and_why(authed, db, settings) -> None:
    """A capture that found six embeds and was refused five leaves one file and
    five explanations, and the explanations are the point."""
    site_id, _, _ = _site_with_media(
        db,
        settings,
        items=[
            {
                "url": "http://a.test/1",
                "status": "downloaded",
                "filename": "generic-1.mp4",
                "bytes": 2048,
                "title": "One",
            },
            {
                "url": "http://lan.test/2",
                "status": "skipped",
                "reason": "lan.test resolves to 10.0.0.5, which is not a public address",
            },
        ],
    )
    body = authed.get(f"/api/sites/{site_id}/media").json()
    assert len(body["items"]) == 2
    refused = [i for i in body["items"] if i["status"] == "skipped"]
    assert "not a public address" in refused[0]["reason"]
    assert body["total_bytes"] == 2048


def test_a_recorded_file_that_is_gone_is_not_offered_as_playable(authed, db, settings) -> None:
    """Retention or a hand-deletion can remove the file while the manifest
    keeps the record. The record stays; the link does not."""
    site_id, _, _ = _site_with_media(
        db,
        settings,
        items=[
            {
                "url": "http://a.test/1",
                "status": "downloaded",
                "filename": "generic-1.mp4",
                "bytes": 2048,
            }
        ],
    )
    item = authed.get(f"/api/sites/{site_id}/media").json()["items"][0]
    assert item["playable"] is False


def test_a_downloaded_file_can_be_played_back(authed, db, settings) -> None:
    site_id, archive_path, capture_dir = _site_with_media(
        db,
        settings,
        items=[
            {
                "url": "http://a.test/1",
                "status": "downloaded",
                "filename": "generic-1.mp4",
                "bytes": len(MP4),
            }
        ],
    )
    target = media.media_dir(settings, archive_path, capture_dir)
    target.mkdir(parents=True, exist_ok=True)
    (target / "generic-1.mp4").write_bytes(MP4)

    assert authed.get(f"/api/sites/{site_id}/media").json()["items"][0]["playable"] is True

    response = authed.get(f"/api/sites/{site_id}/media/{capture_dir}/generic-1.mp4")
    assert response.status_code == 200
    assert response.content == MP4
    assert response.headers["content-type"] == "video/mp4"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_the_media_server_will_not_serve_anything_it_could_be_tricked_into(
    authed, db, settings
) -> None:
    """Two guards, and they are not interchangeable.

    The extension allowlist is what stops a served file choosing to be HTML —
    yt-dlp names the file from the remote's `%(ext)s`, so the extension is not
    ours. `resolve_within` is what stops it being a file outside the media
    directory.

    Testing traversal with a `.yaml` target proves only the first guard, since
    the extension check fires before the path is ever resolved — measured, and
    it is why the escape attempts below all end in `.mp4`. That is also the
    shape a real one would take: the point of escaping is to be served, and
    only an allowlisted extension is served.
    """
    site_id, archive_path, capture_dir = _site_with_media(db, settings)
    target = media.media_dir(settings, archive_path, capture_dir)
    target.mkdir(parents=True, exist_ok=True)
    (target / "evil.html").write_bytes(b"<script>alert(1)</script>")

    assert authed.get(f"/api/sites/{site_id}/media/{capture_dir}/evil.html").status_code == 404

    # A real file outside the media directory, so an escape that worked would
    # return something recognisable rather than a 404 for being absent.
    outside = settings.data_dir / "escaped.mp4"
    outside.write_bytes(b"NOT-YOURS")

    # Refused, rather than a particular status. The HTTP client collapses dot
    # segments before the request is sent, so several of these never reach the
    # handler at all and come back 422 from some other route — pinning 404
    # would be asserting on httpx's URL normalisation rather than on this
    # application. What matters is that none of them return the file.
    for attempt in (
        "../../../../escaped.mp4",
        "..%2f..%2f..%2f..%2fescaped.mp4",
        "sub/../../../../escaped.mp4",
        "..\\..\\..\\..\\escaped.mp4",
    ):
        response = authed.get(f"/api/sites/{site_id}/media/{capture_dir}/{attempt}")
        assert response.status_code != 200, attempt
        assert b"NOT-YOURS" not in response.content, attempt

    # And this is the assertion that actually exercises the guard: the same
    # strings handed straight to the resolver, with no router in between.
    for attempt in ("../../../../escaped.mp4", "sub/../../../../escaped.mp4"):
        with pytest.raises(storage.StoragePathError):
            media.file_path(settings, archive_path, capture_dir, attempt)


def test_the_instance_default_is_settable_and_sites_inherit_it(authed, db, settings) -> None:
    """The same gap one level up: `media.download` was in DEFAULT_SETTINGS and
    read by `policy_for`, and nothing wrote it either."""
    site_id, _, _ = _site_with_media(db, settings)
    assert authed.get("/api/media/settings").json()["policy"]["enabled"] is False

    saved = authed.put(
        "/api/media/settings", json={"enabled": True, "max_items": 3}, headers=XHR
    ).json()
    assert saved["policy"]["enabled"] is True
    assert saved["override"] == {"enabled": True, "max_items": 3}

    # A site that has said nothing of its own now inherits it.
    inherited = authed.get(f"/api/sites/{site_id}/media").json()
    assert inherited["policy"]["enabled"] is True
    assert inherited["policy"]["max_items"] == 3
    assert inherited["override"] == {}
    assert inherited["instance"] == {"enabled": True, "max_items": 3}


def test_a_site_that_said_no_keeps_saying_no(authed, db, settings) -> None:
    """Turning the instance default on must not switch media on for a site
    somebody deliberately turned it off for — otherwise one checkbox starts
    downloading video across an existing archive."""
    site_id, _, _ = _site_with_media(db, settings)
    authed.put(f"/api/sites/{site_id}/media", json={"enabled": False}, headers=XHR)
    authed.put("/api/media/settings", json={"enabled": True}, headers=XHR)

    site = authed.get(f"/api/sites/{site_id}/media").json()
    assert site["policy"]["enabled"] is False
    assert site["instance"] == {"enabled": True}


def test_clearing_the_instance_default_restores_the_built_in(authed, db, settings) -> None:
    authed.put("/api/media/settings", json={"enabled": True, "max_items": 3}, headers=XHR)
    cleared = authed.put("/api/media/settings", json={}, headers=XHR).json()
    assert cleared["override"] == {}
    assert cleared["policy"]["enabled"] is False
    assert cleared["policy"]["max_items"] == media.DEFAULT_POLICY["max_items"]


def test_the_instance_default_is_bounded_too(authed) -> None:
    for bad in ({"max_items": -1}, {"max_total_bytes": 10**15}, {"format": ""}):
        response = authed.put("/api/media/settings", json=bad, headers=XHR)
        assert response.status_code == 422, (bad, response.text)
