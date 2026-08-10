"""End-to-end capture: API request through to a WARC with real content in it.

This is M1's exit criterion in test form. It runs the actual supervisor, the
actual engine subprocess, and the actual wget against a real HTTP server, then
reads the resulting WARC back with warcio — because a test that only checks
the file exists would pass just as happily on an archive full of interstitials,
which is precisely the failure this whole project exists to avoid.

Skipped where wget is unavailable (Windows dev machines). CI and the container
run it for real.
"""

from __future__ import annotations

import os
import shutil
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from cairn.config import Settings
from cairn.services import storage
from tests.conftest import XHR

# Skipped on Windows even when a wget is on PATH. Git for Windows ships a
# mingw32 build whose WARC temp files hit the 260-character MAX_PATH limit
# inside pytest's own tmp_path, failing with "Could not open temporary WARC
# manifest file" — an artefact of the harness, not of the code under test.
# The deployment target is Linux, so this runs for real in the container and
# in CI. Verified: a 119-character temp directory fails, 55 succeeds.
pytestmark = [
    pytest.mark.skipif(shutil.which("wget") is None, reason="needs GNU wget on PATH"),
    pytest.mark.skipif(
        os.name == "nt", reason="mingw wget hits MAX_PATH under pytest tmp_path; runs in Docker/CI"
    ),
]

CAPTURE_TIMEOUT_S = 120
POLL_S = 0.25

PAGES: dict[str, tuple[str, bytes]] = {
    "/": (
        "text/html",
        b"""<html><body><h1>Index</h1>
        <a href="/post-1.html">one</a>
        <a href="/post-2.html">two</a>
        <a href="/missing.html">gone</a>
        <img src="/logo.png">
        </body></html>""",
    ),
    "/post-1.html": (
        "text/html",
        b"<html><head><style>"
        # Exactly how a Blogger skin writes its theme image. wget does not
        # decode CSS escapes, so it requests this against the blog and 404s.
        rb"body{background:url(https\:\/\/themes.example.test\/image?id=abc)}"
        b"</style></head><body><h1>Post One</h1><p>UNIQUE-CONTENT-MARKER-ONE</p>"
        b'<img data-src="/lazy.png"></body></html>',
    ),
    "/post-2.html": (
        "text/html",
        b"<html><body><h1>Post Two</h1><p>UNIQUE-CONTENT-MARKER-TWO</p></body></html>",
    ),
    "/logo.png": ("image/png", b"\x89PNG\r\n\x1a\n" + b"LOGO" * 32),
}


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        path = self.path.split("?")[0]
        if path == "/robots.txt":
            body = b"User-agent: *\nAllow: /\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        hit = PAGES.get(path)
        if hit is None:
            body = b"<html><body>not found</body></html>"
            self.send_response(404)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        ctype, body = hit
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        pass


@pytest.fixture
def site_server() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/"
    finally:
        server.shutdown()
        server.server_close()


def wait_for_job(client: TestClient, job_id: int) -> dict[str, Any]:
    deadline = time.monotonic() + CAPTURE_TIMEOUT_S
    while time.monotonic() < deadline:
        res = client.get(f"/api/jobs/{job_id}")
        assert res.status_code == 200, res.text
        job: dict[str, Any] = res.json()
        if job["status"] not in ("queued", "running"):
            return job
        time.sleep(POLL_S)
    raise AssertionError(f"job {job_id} did not finish within {CAPTURE_TIMEOUT_S}s")


def run_capture(client: TestClient, seed: str, **overrides: Any) -> tuple[dict[str, Any], int]:
    created = client.post(
        "/api/sites", json={"seed_url": seed, "title": "Fixture Site", **overrides}, headers=XHR
    )
    assert created.status_code == 201, created.text
    site_id = created.json()["id"]

    # The fixture server has no robots.txt restrictions, but wget fetches it
    # first regardless; leaving robots on keeps the default path under test.
    started = client.post(f"/api/sites/{site_id}/capture", json={"kind": "full"}, headers=XHR)
    assert started.status_code == 202, started.text
    job = wait_for_job(client, started.json()["job_id"])
    # A failing capture must say why here; without the error text this reads
    # as "'failed' != 'ok'", which is the least useful sentence in testing.
    assert job["status"] == "ok", f"capture failed: {job.get('error')!r}"
    return job, site_id


# ── the exit criterion ───────────────────────────────────────────────────


def test_capture_produces_a_warc_with_real_content(
    authed: TestClient, settings: Settings, site_server: str
) -> None:
    from warcio.archiveiterator import ArchiveIterator

    job, site_id = run_capture(authed, site_server)
    assert job["status"] == "ok", job

    captures = authed.get(f"/api/sites/{site_id}/captures").json()
    assert len(captures) == 1
    capture = captures[0]
    assert capture["status"] in ("ok", "partial")
    assert capture["url_count"] >= 4

    site = authed.get(f"/api/sites/{site_id}").json()
    capture_dir = (
        storage.site_dir(settings, site["archive_path"])
        / storage.CAPTURES_DIR
        / capture["dir_name"]
    )

    warcs = sorted((capture_dir / storage.WARC_DIR).glob("*.warc.gz"))
    assert warcs, "no WARC was written"

    bodies = b""
    urls: set[str] = set()
    for warc in warcs:
        with open(warc, "rb") as fh:
            for record in ArchiveIterator(fh):
                if record.rec_type != "response":
                    continue
                urls.add(record.rec_headers.get_header("WARC-Target-URI"))
                bodies += record.content_stream().read()

    # The payload is the point: real page text, not a placeholder or a warning.
    assert b"UNIQUE-CONTENT-MARKER-ONE" in bodies
    assert b"UNIQUE-CONTENT-MARKER-TWO" in bodies
    assert any(u.endswith("/logo.png") for u in urls), "page requisites were not captured"


def test_each_page_is_fetched_once(authed: TestClient, site_server: str) -> None:
    """Seeds must reach wget through exactly one channel.

    Passing the same URL in --input-file *and* on the command line makes wget
    queue it twice and crawl the entire site twice — double the time, double
    the WARC, no error. The first real capture did exactly that.
    """
    _job, site_id = run_capture(authed, site_server)
    capture = authed.get(f"/api/sites/{site_id}/captures").json()[0]

    rows = authed.get(f"/api/captures/{capture['id']}/urls?per_page=500").json()["items"]
    urls = [row["url"] for row in rows]
    duplicated = {u for u in urls if urls.count(u) > 1}
    assert not duplicated, f"these URLs were fetched more than once: {sorted(duplicated)}"


def test_many_seeds_do_not_multiply_the_crawl(authed: TestClient, site_server: str) -> None:
    """Handing the crawler one seed per page must not re-crawl per seed.

    wget's on-disk mirror is how it remembers what it already has. With
    --delete-after each file vanishes immediately, so every extra seed
    rediscovers the whole site as new — 4.8x the records for six seeds,
    measured. Discovery supplies one seed per post, so this scales with the
    size of the blog and shows up nowhere in the log.
    """
    created = authed.post(
        "/api/sites", json={"seed_url": site_server, "title": "Many Seeds"}, headers=XHR
    )
    site_id = created.json()["id"]

    extra = [f"{site_server.rstrip('/')}{path}" for path in ("/post-1.html", "/post-2.html")]
    started = authed.post(
        f"/api/sites/{site_id}/capture",
        json={"kind": "full", "extra_seeds": extra},
        headers=XHR,
    )
    assert started.status_code == 202, started.text
    job = wait_for_job(authed, started.json()["job_id"])
    assert job["status"] == "ok", f"capture failed: {job.get('error')!r}"

    capture = authed.get(f"/api/sites/{site_id}/captures").json()[0]
    rows = authed.get(f"/api/captures/{capture['id']}/urls?per_page=500").json()["items"]

    # Only successful fetches are checked. A request that failed wrote no file,
    # so it left no dedup record either and a later seed may retry it — which
    # is bounded, cheap, and arguably right, since a 404 can be transient. The
    # property that matters is that archived content is not duplicated.
    fetched = [r["url"] for r in rows if not r["error"] and (r["status_code"] or 0) < 400]
    repeated = {u: fetched.count(u) for u in set(fetched) if fetched.count(u) > 1}
    assert not repeated, f"seeds multiplied the crawl: {repeated}"
    assert len(fetched) >= 4, "the capture fetched almost nothing; the assertion is vacuous"


def test_capture_writes_a_manifest_with_verified_checksums(
    authed: TestClient, settings: Settings, site_server: str
) -> None:
    """manifest.json plus site.yaml is what makes the database rebuildable."""
    import hashlib

    job, site_id = run_capture(authed, site_server)
    assert job["status"] == "ok"

    site = authed.get(f"/api/sites/{site_id}").json()
    site_root = storage.site_dir(settings, site["archive_path"])
    assert (site_root / storage.SITE_FILE).is_file()

    capture = authed.get(f"/api/sites/{site_id}/captures").json()[0]
    capture_dir = site_root / storage.CAPTURES_DIR / capture["dir_name"]
    manifest = storage.read_json(capture_dir / storage.MANIFEST_FILE)

    assert manifest["schema"] == 1
    assert manifest["status"] in ("ok", "partial")
    assert manifest["engine"]["id"] == "wget-warc"
    assert manifest["engine"]["tool_version"], "the wget version was not recorded"
    assert manifest["scope"]["hosts"], "the capture's boundary was not recorded"

    warc_entries = [a for a in manifest["warc_files"] if a["kind"] == "warc"]
    assert warc_entries, "no WARC artifact was recorded"
    for artifact in manifest["warc_files"]:
        path = capture_dir / artifact["name"]
        assert path.is_file(), f"{artifact['name']} is recorded but missing"
        # Computed here, not taken from the engine's word for it.
        assert artifact["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
        assert artifact["size"] == path.stat().st_size


def test_capture_records_urls_and_surfaces_the_404(authed: TestClient, site_server: str) -> None:
    """A partial result is a first-class outcome: the gaps must be listed."""
    _job, site_id = run_capture(authed, site_server)
    capture = authed.get(f"/api/sites/{site_id}/captures").json()[0]

    listed = authed.get(f"/api/captures/{capture['id']}/urls").json()
    assert listed["total"] >= 4
    fetched = {row["url"] for row in listed["items"]}
    assert any(u.endswith("/post-1.html") for u in fetched)

    errors = authed.get(f"/api/captures/{capture['id']}/urls?errors_only=true").json()
    assert errors["total"] >= 1
    assert any(row["status_code"] == 404 for row in errors["items"])


def test_site_stats_roll_up_after_a_capture(authed: TestClient, site_server: str) -> None:
    run_capture(authed, site_server)
    site = authed.get("/api/sites").json()["items"][0]
    assert site["size_bytes"] > 0
    assert site["url_count"] > 0
    assert site["status"] == "ready"
    assert site["last_capture_at"] is not None


def test_crawl_log_is_readable_through_the_api(authed: TestClient, site_server: str) -> None:
    _job, site_id = run_capture(authed, site_server)
    capture = authed.get(f"/api/sites/{site_id}/captures").json()[0]
    log = authed.get(f"/api/captures/{capture['id']}/log").text
    assert "post-1.html" in log


def test_lazy_images_are_reported_rather_than_left_to_be_discovered(
    authed: TestClient, settings: Settings, site_server: str
) -> None:
    """wget cannot execute JavaScript, so data-src images are simply absent.
    Saying so now beats finding out during replay months later."""
    _job, site_id = run_capture(authed, site_server)
    site = authed.get(f"/api/sites/{site_id}").json()
    capture = authed.get(f"/api/sites/{site_id}/captures").json()[0]
    manifest = storage.read_json(
        storage.site_dir(settings, site["archive_path"])
        / storage.CAPTURES_DIR
        / capture["dir_name"]
        / storage.MANIFEST_FILE
    )
    assert manifest["stats"].get("lazy_image_hints", 0) >= 1
    assert any("lazy-loaded" in w for w in manifest["stats"].get("warnings", []))


def test_css_escaped_urls_are_explained_rather_than_left_as_404s(
    authed: TestClient, settings: Settings, site_server: str
) -> None:
    r"""Blogger skins write theme images as url(https\:\/\/host\/x.png).

    wget does not decode CSS escapes, so it treats that absolute URL as
    relative and requests it against the blog, producing a 404 whose cause is
    not remotely obvious from the log. The capture should say what happened
    and name the host the asset was really on.
    """
    _job, site_id = run_capture(authed, site_server)
    site = authed.get(f"/api/sites/{site_id}").json()
    capture = authed.get(f"/api/sites/{site_id}/captures").json()[0]

    manifest = storage.read_json(
        storage.site_dir(settings, site["archive_path"])
        / storage.CAPTURES_DIR
        / capture["dir_name"]
        / storage.MANIFEST_FILE
    )
    stats = manifest["stats"]
    assert stats.get("css_escaped_failures", 0) >= 1

    warnings = " ".join(stats.get("warnings", []))
    assert "CSS-escaped" in warnings
    assert "themes.example.test" in warnings


def test_second_capture_of_the_same_site_gets_its_own_directory(
    authed: TestClient, site_server: str
) -> None:
    _job, site_id = run_capture(authed, site_server)
    second = authed.post(f"/api/sites/{site_id}/capture", json={"kind": "full"}, headers=XHR)
    assert second.status_code == 202
    wait_for_job(authed, second.json()["job_id"])

    captures = authed.get(f"/api/sites/{site_id}/captures").json()
    assert len({c["dir_name"] for c in captures}) == len(captures) == 2


def test_a_capture_cannot_be_started_twice_at_once(authed: TestClient, site_server: str) -> None:
    created = authed.post(
        "/api/sites", json={"seed_url": site_server, "title": "Busy"}, headers=XHR
    )
    site_id = created.json()["id"]
    first = authed.post(f"/api/sites/{site_id}/capture", json={"kind": "full"}, headers=XHR)
    assert first.status_code == 202
    second = authed.post(f"/api/sites/{site_id}/capture", json={"kind": "full"}, headers=XHR)
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "already_running"
    wait_for_job(authed, first.json()["job_id"])


def test_temp_directory_is_cleaned_up(
    authed: TestClient, settings: Settings, site_server: str
) -> None:
    """Plaintext cookie jars live there for the life of the job."""
    job, _site_id = run_capture(authed, site_server)
    leftovers = list(settings.tmp_dir.glob("job-*"))
    assert leftovers == [], f"job directory survived: {leftovers}"
    assert job["status"] == "ok"


def test_events_stream_replays_history_then_closes(authed: TestClient, site_server: str) -> None:
    """A reconnecting log viewer must get what it missed, then be let go.

    Replaying and then blocking forever holds a connection and a server task
    open for every finished job anyone opens, and the client never learns that
    nothing more is coming. The stream must end on its own.
    """
    job, _site_id = run_capture(authed, site_server)

    deadline = time.monotonic() + 30
    seen = ""
    with authed.stream("GET", f"/api/jobs/{job['id']}/events", headers=XHR) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        for chunk in response.iter_text():
            seen += chunk
            if time.monotonic() > deadline:
                raise AssertionError("the event stream did not close on its own")

    assert "event: " in seen
    assert "event: status" in seen


def test_capture_directory_layout_matches_the_documented_shape(
    authed: TestClient, settings: Settings, site_server: str
) -> None:
    _job, site_id = run_capture(authed, site_server)
    site = authed.get(f"/api/sites/{site_id}").json()
    root: Path = storage.site_dir(settings, site["archive_path"])

    assert (root / "captures").is_dir()
    assert (root / "index").is_dir()
    assert (root / "derived").is_dir()
    assert (root / "exports").is_dir()

    capture_dir = next((root / "captures").iterdir())
    assert storage.is_capture_dir_name(capture_dir.name)
    assert (capture_dir / "warc").is_dir()
    assert (capture_dir / "crawl.log").is_file()
    assert (capture_dir / "manifest.json").is_file()
