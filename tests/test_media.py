"""Finding and downloading the media an archived post embedded.

Every fetch here is against a fixture on this machine. yt-dlp will happily go
to YouTube if you hand it a YouTube URL — the probe that established what this
can do without ffmpeg did exactly that once, by following an embed in a test
page — so the tests are written so that cannot happen: the only URLs offered
to the downloader are the fixture's own.
"""

from __future__ import annotations

import shutil
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from cairn.services import media

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
        scope = authed.get(f"/api/sites/{site_id}").json()
        assert scope is not None
        from cairn.db.models import Site

        factory = authed.app.state.sessionmaker
        with factory() as session:
            site = session.get(Site, site_id)
            site.scope_settings = {
                **(site.scope_settings or {}),
                "media": {"enabled": True, "allow_private_hosts": True},
            }
            session.commit()

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
