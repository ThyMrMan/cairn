"""Diffing, watching and retention, end to end against real captures.

Three claims:

  **A recapture's changes are visible.** A real wget captures a blog, one post
  is edited, a second capture runs, and the diff names that post and the
  sentence inside it — and says the other pages are unchanged, which is the
  answer to "was that recapture worth its disk?".

  **A watched page is captured when its text changes, not when its markup
  does.** The fixture rotates a visit counter and a timestamp on every request.

  **Retention refuses to delete the last copy.** A post is removed from the
  live site between captures; retention then protects the capture holding it,
  and reports why.

Needs GNU wget, so it runs in the container and in CI.
"""

from __future__ import annotations

import os
import shutil
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

from cairn.config import Settings
from cairn.services import storage
from tests.conftest import XHR
from tests.test_capture_e2e import run_capture, wait_for_job

pytestmark = [
    pytest.mark.skipif(shutil.which("wget") is None, reason="needs GNU wget on PATH"),
    pytest.mark.skipif(
        os.name == "nt", reason="mingw wget hits MAX_PATH under pytest tmp_path; runs in Docker/CI"
    ),
]

ORIGINAL = "The wind on Harris was steady enough to blur a six-second exposure into fog."
EDITED = "The wind on Harris was steady enough to blur an eight-second exposure into fog."


class _Blog:
    """A small blog that can be edited while the test runs.

    `visits` increments on every request, so anything hashing the response
    body sees a change on every poll — which is what the watcher must not do.
    """

    def __init__(self) -> None:
        self.body = ORIGINAL
        self.extra_post: str | None = "corncrake"
        self.visits = 0

    def page(self, slug: str) -> bytes:
        self.visits += 1
        links = "".join(
            f"<li><a href='/{s}.html'>{s.title()}</a></li>"
            for s in ["harris", "tarbert", *([self.extra_post] if self.extra_post else [])]
        )
        body = self.body if slug == "harris" else f"Notes about {slug}."
        return f"""<!DOCTYPE html><html><head><title>Coast: {slug}</title></head><body>
<div class='navbar section'><a href='/'>Home</a><span>Visits: {self.visits}</span></div>
<div class='main section'><div class='post hentry'>
<h3 class='post-title entry-title'>{slug.title()}</h3>
<div class='post-body entry-content'><p>{body}</p></div>
<div class='post-footer'>Posted at 2026-08-11T20:{self.visits % 60:02d}:00Z</div>
</div></div>
<div class='sidebar section'><ul>{links}</ul></div>
</body></html>""".encode()

    def index(self) -> bytes:
        self.visits += 1
        links = "".join(
            f"<li><a href='/{s}.html'>{s.title()}</a></li>"
            for s in ["harris", "tarbert", *([self.extra_post] if self.extra_post else [])]
        )
        return f"""<!DOCTYPE html><html><head><title>Coast</title></head><body>
<div class='main section'><h1>Coast</h1><ul>{links}</ul></div></body></html>""".encode()


@pytest.fixture
def blog() -> Iterator[tuple[str, _Blog]]:
    state = _Blog()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:
            path = self.path.split("?")[0]
            if path == "/robots.txt":
                self._send(b"User-agent: *\nAllow: /\n", "text/plain")
                return
            if path == "/":
                self._send(state.index(), "text/html")
                return
            slug = path.removeprefix("/").removesuffix(".html")
            known = {"harris", "tarbert", *([state.extra_post] if state.extra_post else [])}
            if slug in known:
                self._send(state.page(slug), "text/html")
                return
            self._send(b"<html><body>gone</body></html>", "text/html", code=404)

        def _send(self, body: bytes, ctype: str, code: int = 200) -> None:
            self.send_response(code)
            self.send_header("Content-Type", f"{ctype}; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            # No validators: a conditional GET answered 304 would make the
            # watcher test prove nothing about hashing.
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/", state
    finally:
        server.shutdown()
        server.server_close()


def recapture(client: TestClient, site_id: int) -> None:
    started = client.post(f"/api/sites/{site_id}/capture", json={"kind": "full"}, headers=XHR)
    assert started.status_code == 202, started.text
    job = wait_for_job(client, started.json()["job_id"])
    assert job["status"] == "ok", job


# ── diffing ──────────────────────────────────────────────────────────────


def test_the_diff_names_the_post_that_changed(
    authed: TestClient, settings: Settings, blog: tuple[str, _Blog]
) -> None:
    url, state = blog
    _job, site_id = run_capture(authed, url)

    state.body = EDITED
    recapture(authed, site_id)

    diff = authed.get(f"/api/sites/{site_id}/diff").json()
    assert diff["changed"] >= 1
    changed = [p for p in diff["pages"] if p["kind"] == "changed"]
    assert any(p["url"].endswith("/harris.html") for p in changed)
    # The other post did not change, and the diff has to say so — that is the
    # number that answers "was this recapture worth it?".
    assert diff["unchanged"] >= 1
    assert not any(p["url"].endswith("/tarbert.html") for p in changed)

    page = authed.get(f"/api/sites/{site_id}/diff/page", params={"url": f"{url}harris.html"}).json()
    assert page["changed"] is True
    edits = [w for block in page["blocks"] for w in block["words"]]
    assert any("eight-second" in w["after"] for w in edits)
    assert any("six-second" in w["before"] for w in edits)


def test_rotating_furniture_does_not_read_as_a_change(
    authed: TestClient, settings: Settings, blog: tuple[str, _Blog]
) -> None:
    """Every response carries a different visit count and timestamp. Diffing
    the markup would report every page as changed on every capture."""
    url, _state = blog
    _job, site_id = run_capture(authed, url)
    recapture(authed, site_id)

    diff = authed.get(f"/api/sites/{site_id}/diff").json()
    assert diff["changed"] == 0, [p["url"] for p in diff["pages"]]
    assert diff["added"] == 0
    assert diff["removed"] == 0
    assert diff["unchanged"] >= 3


def test_comparing_needs_two_captures(
    authed: TestClient, settings: Settings, blog: tuple[str, _Blog]
) -> None:
    url, _state = blog
    _job, site_id = run_capture(authed, url)
    res = authed.get(f"/api/sites/{site_id}/diff")
    assert res.status_code == 409
    assert "two finished captures" in res.json()["error"]["message"]


# ── the page watcher ─────────────────────────────────────────────────────


def test_a_watched_page_is_captured_when_its_text_changes(
    authed: TestClient, settings: Settings, blog: tuple[str, _Blog], db
) -> None:
    """The feedless-site story: no feed, a page that changes, a capture."""
    url, state = blog
    _job, site_id = run_capture(authed, url)

    added = authed.post(
        f"/api/sites/{site_id}/feeds",
        json={"url": f"{url}harris.html", "kind": "page"},
        headers=XHR,
    )
    assert added.status_code == 201, added.text
    feed_id = added.json()["id"]

    # First poll is a baseline: it records the page and captures nothing.
    first = authed.post(f"/api/feeds/{feed_id}/poll", headers=XHR).json()
    assert first["new_items"] == 0

    # A poll with nothing changed but the visit counter and the timestamp.
    quiet = authed.post(f"/api/feeds/{feed_id}/poll", headers=XHR).json()
    assert quiet["new_items"] == 0, "the furniture moved and the watcher noticed"

    state.body = EDITED
    noisy = authed.post(f"/api/feeds/{feed_id}/poll", headers=XHR).json()
    assert noisy["new_items"] == 1
    # The poll captures what it found, exactly as the ticker would have.
    assert noisy["job_ids"], noisy
    job = wait_for_job(authed, noisy["job_ids"][0])
    assert job["status"] == "ok", job

    captures = authed.get(f"/api/sites/{site_id}/captures").json()
    assert len(captures) == 2
    assert captures[0]["kind"] == "feed"

    # And the new capture holds the edited text, not the old.
    diff = authed.get(f"/api/sites/{site_id}/diff/page", params={"url": f"{url}harris.html"}).json()
    assert any("eight-second" in w["after"] for block in diff["blocks"] for w in block["words"])


def test_a_watcher_records_every_poll(
    authed: TestClient, settings: Settings, blog: tuple[str, _Blog]
) -> None:
    url, _state = blog
    _job, site_id = run_capture(authed, url)
    added = authed.post(
        f"/api/sites/{site_id}/feeds",
        json={"url": f"{url}harris.html", "kind": "page"},
        headers=XHR,
    )
    feed_id = added.json()["id"]
    authed.post(f"/api/feeds/{feed_id}/poll", headers=XHR)
    authed.post(f"/api/feeds/{feed_id}/poll", headers=XHR)

    polls = authed.get(f"/api/feeds/{feed_id}/polls").json()
    assert len(polls) == 2
    assert polls[0]["status"] == 200
    assert "baseline" in polls[-1]["action"]


# ── retention ────────────────────────────────────────────────────────────


def test_retention_will_not_delete_the_only_copy_of_a_deleted_post(
    authed: TestClient, settings: Settings, blog: tuple[str, _Blog], db
) -> None:
    """The clause the whole feature exists for, against a real site that
    really loses a page."""
    url, state = blog
    _job, site_id = run_capture(authed, url)
    recapture(authed, site_id)

    # The author deletes a post. Everything after this is an archive of a
    # smaller site, and the two captures above are the only copies of it.
    state.extra_post = None
    recapture(authed, site_id)
    recapture(authed, site_id)

    captures = authed.get(f"/api/sites/{site_id}/captures").json()
    assert len(captures) == 4

    saved = authed.put(
        f"/api/sites/{site_id}/retention",
        json={"enabled": True, "keep_last": 1, "keep_monthly": 0, "min_age_days": 0},
        headers=XHR,
    )
    assert saved.status_code == 200, saved.text
    plan = saved.json()

    by_dir = {d["dir_name"]: d for d in plan["captures"]}
    oldest = sorted(by_dir)
    # The second capture is the last one holding the deleted post; it stays,
    # and the plan says exactly why.
    protector = by_dir[oldest[1]]
    assert protector["keep"] is True
    assert protector["reason"] == "last-copy"
    assert "corncrake" in protector["detail"]

    # And applying the plan leaves it alone.
    queued = authed.post(f"/api/sites/{site_id}/retention/apply", headers=XHR)
    assert queued.status_code == 202
    job = wait_for_job(authed, queued.json()["job_id"])
    assert job["status"] == "ok", job

    remaining = {c["dir_name"] for c in authed.get(f"/api/sites/{site_id}/captures").json()}
    assert oldest[0] in remaining, "the first capture must never be pruned"
    assert oldest[1] in remaining, "the last copy of a deleted post must never be pruned"

    root = storage.site_dir(settings, authed.get(f"/api/sites/{site_id}").json()["archive_path"])
    on_disk = {p.name for p in (root / storage.CAPTURES_DIR).iterdir()}
    assert on_disk == remaining, "the database and the disk disagree about what survived"


def test_retention_is_off_until_it_is_switched_on(
    authed: TestClient, settings: Settings, blog: tuple[str, _Blog]
) -> None:
    url, _state = blog
    _job, site_id = run_capture(authed, url)
    recapture(authed, site_id)

    plan = authed.get(f"/api/sites/{site_id}/retention").json()
    assert plan["policy"]["enabled"] is False
    assert len(plan["captures"]) == 2

    queued = authed.post(f"/api/sites/{site_id}/retention/apply", headers=XHR)
    job = wait_for_job(authed, queued.json()["job_id"])
    assert job["status"] == "ok"
    assert job["progress"]["pruned"] == 0
    assert len(authed.get(f"/api/sites/{site_id}/captures").json()) == 2
