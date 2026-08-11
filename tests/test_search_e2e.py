"""M8's exit criteria, end to end against real tools.

Three claims, each asserted against a real capture rather than a fixture
database:

  **Search finds the post, not the blog.** A real wget crawls a real blog
  whose sidebar lists every post title on every page. Searching one of those
  titles must return one result. A naive index returns every page, which is
  not a ranking problem — the answer is simply wrong.

  **The export replays.** The WACZ is handed to a real pywb, which unpacks it,
  reads *our* index, and serves a page back out of *our* archive file. That is
  an independent reader resolving our offsets, which is the only thing that
  makes a WACZ a WACZ.

  **Verification notices damage.** One byte is flipped in an archived WARC and
  the verify job says which file, in which capture.

Needs GNU wget and pywb, so it runs in the container and in CI.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
import zipfile
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cairn.config import Settings
from cairn.services import replay, storage
from tests.conftest import XHR
from tests.test_capture_e2e import run_capture, wait_for_job

pytestmark = [
    pytest.mark.skipif(shutil.which("wget") is None, reason="needs GNU wget on PATH"),
    pytest.mark.skipif(
        os.name == "nt", reason="mingw wget hits MAX_PATH under pytest tmp_path; runs in Docker/CI"
    ),
]

REPLAY_PORT = 8898
STARTUP_TIMEOUT_S = 30

# One phrase that appears on exactly one page, and post titles that appear on
# every page because the sidebar lists them.
POSTS = {
    "harris": (
        "Long exposures on Harris",
        "The wind on Harris was steady enough to blur a six-second exposure into fog. "
        "I had gone looking for the machair in flower and found a tide that would not settle.",
    ),
    "luskentyre": (
        "Three days of rain at Luskentyre",
        "The sand goes the colour of weak tea when it is wet, which no photograph I have "
        "taken manages to show.",
    ),
    "tarbert": (
        "An afternoon in Tarbert",
        "A ferry cancelled, so the harbour wall instead. It is the only shelter and "
        "everyone on the island knows it.",
    ),
    "filters": (
        "Notes on neutral density",
        "A ten-stop is a blunt instrument and I keep reaching for it anyway, mostly out "
        "of habit rather than judgement.",
    ),
    "corncrake": (
        "The corncrake I never saw",
        "Heard from three fields away for a fortnight and never once in view, which is "
        "apparently the usual arrangement.",
    ),
}
UNIQUE_PHRASE = "machair in flower"
SIDEBAR_TITLE = "The corncrake I never saw"


def blog_page(slug: str) -> bytes:
    title, body = POSTS[slug]
    sidebar = "".join(f"<li><a href='/{s}.html'>{t}</a></li>" for s, (t, _) in POSTS.items())
    return f"""<!DOCTYPE html><html><head><meta content='blogger' name='generator'/>
<title>Coast &amp; Light: {title}</title></head><body>
<div class='navbar section' id='navbar'><a href='/'>Home</a>
<a href='/about.html'>About this blog</a></div>
<div class='main section' id='main'><div class='widget Blog'>
<div class='post hentry'><h3 class='post-title entry-title'>{title}</h3>
<div class='post-body entry-content'><p>{body}</p></div>
<div class='post-footer'>Posted by Ali</div></div></div></div>
<div class='sidebar section' id='sidebar-right-1'>
<div class='widget BlogArchive'><h2>Blog Archive</h2><ul>{sidebar}</ul></div></div>
<div class='footer section' id='footer'>Powered by Blogger.</div>
</body></html>""".encode()


def index_page() -> bytes:
    links = "".join(f"<li><a href='/{s}.html'>{t}</a></li>" for s, (t, _) in POSTS.items())
    return f"""<!DOCTYPE html><html><head><title>Coast &amp; Light</title></head><body>
<div class='main section'><h1>Coast &amp; Light</h1><ul>{links}</ul></div>
</body></html>""".encode()


class _BlogHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        path = self.path.split("?")[0]
        if path == "/robots.txt":
            self._send(b"User-agent: *\nAllow: /\n", "text/plain")
            return
        if path == "/":
            self._send(index_page(), "text/html")
            return
        slug = path.removeprefix("/").removesuffix(".html")
        if slug in POSTS:
            self._send(blog_page(slug), "text/html")
            return
        self._send(b"<html><body>not found</body></html>", "text/html", code=404)

    def _send(self, body: bytes, ctype: str, code: int = 200) -> None:
        self.send_response(code)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        pass


@pytest.fixture
def blog() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _BlogHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/"
    finally:
        server.shutdown()
        server.server_close()


def capture_the_blog(client: TestClient, blog: str) -> int:
    _job, site_id = run_capture(client, blog)
    return int(site_id)


# ── search ───────────────────────────────────────────────────────────────


def test_searching_a_post_title_finds_the_post_not_the_blog(
    authed: TestClient, settings: Settings, blog: str
) -> None:
    """The milestone's headline claim.

    `SIDEBAR_TITLE` is in the markup of all six archived pages, because the
    template lists every post in the sidebar of every post. Indexed as it was
    served, this query returns the whole blog.

    It should return two: the post itself, and the index — which is a list of
    every post and therefore genuinely about this one. What it must never
    return is the four unrelated posts, which contain the phrase only as
    furniture.

    Exact set equality, because both directions are failures. More is
    boilerplate leaking in; fewer is content being thrown away with it —
    deleting the class rules produces the second, since the repetition filter
    then drops the index's own list along with the sidebars it matches. The
    two filters are pinned individually in `test_search.py`; this pins the
    outcome.
    """
    capture_the_blog(authed, blog)

    unique = authed.get("/api/search", params={"q": f'"{UNIQUE_PHRASE}"'}).json()
    assert unique["total"] == 1, unique
    assert unique["hits"][0]["url"].endswith("/harris.html")

    titled = authed.get("/api/search", params={"q": f'"{SIDEBAR_TITLE}"'}).json()
    matched = {h["url"] for h in titled["hits"]}
    assert matched == {blog, f"{blog}corncrake.html"}, matched


def test_a_result_carries_what_the_ui_needs_to_open_it(
    authed: TestClient, settings: Settings, blog: str
) -> None:
    site_id = capture_the_blog(authed, blog)
    hit = authed.get("/api/search", params={"q": f'"{UNIQUE_PHRASE}"'}).json()["hits"][0]

    assert hit["site_id"] == site_id
    assert hit["capture_id"] is not None
    assert len(hit["timestamp"]) == 14 and hit["timestamp"].isdigit()
    assert any(UNIQUE_PHRASE in s for s in hit["snippets"])
    assert "Long exposures on Harris" in hit["title"]

    # The timestamp has to name a version replay actually holds, or the link
    # from a search result lands on a 404.
    versions = authed.get(
        f"/api/sites/{site_id}/replay/versions", params={"url": hit["url"]}
    ).json()
    assert hit["timestamp"] in {v["timestamp"] for v in versions["versions"]}


def test_the_extracted_text_is_beside_the_archive(
    authed: TestClient, settings: Settings, blog: str
) -> None:
    """Derived, on disk, and rebuildable: the index is not the only copy."""
    site_id = capture_the_blog(authed, blog)
    site = authed.get(f"/api/sites/{site_id}").json()
    capture = authed.get(f"/api/sites/{site_id}/captures").json()[0]

    path = (
        storage.site_dir(settings, site["archive_path"])
        / storage.DERIVED_DIR
        / "text"
        / f"{capture['dir_name']}.jsonl"
    )
    assert path.is_file()
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == len(POSTS) + 1
    blocks = "\n".join("\n".join(entry["blocks"]) for entry in lines)
    assert UNIQUE_PHRASE in blocks
    assert "Powered by Blogger" not in blocks


def test_reindexing_from_disk_restores_the_index(
    authed: TestClient, settings: Settings, blog: str
) -> None:
    """What a database restore needs: the index rebuilt without the WARCs."""
    site_id = capture_the_blog(authed, blog)
    assert authed.get("/api/search", params={"q": f'"{UNIQUE_PHRASE}"'}).json()["total"] == 1

    site = authed.get(f"/api/sites/{site_id}").json()
    capture = authed.get(f"/api/sites/{site_id}/captures").json()[0]
    warc_dir = (
        storage.site_dir(settings, site["archive_path"])
        / storage.CAPTURES_DIR
        / capture["dir_name"]
        / storage.WARC_DIR
    )
    for warc in warc_dir.glob("*.warc.gz"):
        warc.unlink()

    queued = authed.post("/api/maintenance/reindex-search", headers=XHR)
    assert queued.status_code == 202, queued.text
    job = wait_for_job(authed, queued.json()["job_id"])
    assert job["status"] == "ok", job

    assert authed.get("/api/search", params={"q": f'"{UNIQUE_PHRASE}"'}).json()["total"] == 1


# ── WACZ ─────────────────────────────────────────────────────────────────


def wacz_of(authed: TestClient, settings: Settings, site_id: int) -> Path:
    queued = authed.post(f"/api/sites/{site_id}/export/wacz", headers=XHR)
    assert queued.status_code == 202, queued.text
    job = wait_for_job(authed, queued.json()["job_id"])
    assert job["status"] == "ok", job

    listed = authed.get(f"/api/sites/{site_id}/exports").json()
    assert len(listed) == 1, listed
    site = authed.get(f"/api/sites/{site_id}").json()
    return (
        storage.site_dir(settings, site["archive_path"])
        / storage.EXPORTS_DIR
        / str(listed[0]["name"])
    )


def test_the_export_downloads_as_an_attachment(
    authed: TestClient, settings: Settings, blog: str
) -> None:
    site_id = capture_the_blog(authed, blog)
    path = wacz_of(authed, settings, site_id)

    res = authed.get(f"/api/sites/{site_id}/exports/{path.name}")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("application/wacz")
    # A zip of untrusted archived bytes is never offered to the browser as
    # something to render.
    assert "attachment" in res.headers.get("content-disposition", "")
    assert res.headers.get("x-content-type-options") == "nosniff"
    assert res.content[:2] == b"PK"
    assert len(res.content) == path.stat().st_size


@pytest.mark.skipif(shutil.which("wayback") is None, reason="needs pywb on PATH")
def test_a_real_pywb_replays_the_export(
    authed: TestClient, settings: Settings, blog: str, tmp_path: Path
) -> None:
    """The claim the format exists for, tested by an independent reader.

    pywb unpacks the file, reads *our* index, rewrites the filenames it names,
    and serves a page out of *our* archive member. Nothing about that works
    unless the offsets we recorded land on the records we said they did.
    """
    site_id = capture_the_blog(authed, blog)
    path = wacz_of(authed, settings, site_id)

    root = tmp_path / "import"
    root.mkdir()
    manager = shutil.which("wb-manager") or "wb-manager"
    assert subprocess.run([manager, "init", "imported"], cwd=root).returncode == 0
    added = subprocess.run(
        [manager, "add", "--unpack-wacz", "imported", str(path)],
        cwd=root,
        capture_output=True,
        text=True,
    )
    assert added.returncode == 0, added.stdout + added.stderr

    collection = root / "collections" / "imported"
    assert list((collection / "archive").iterdir()), "pywb unpacked no WARCs"
    index = (collection / "indexes" / "index.cdxj").read_text(encoding="utf-8")
    assert "harris.html" in index

    proc = subprocess.Popen(
        [shutil.which("wayback") or "wayback", "--port", str(REPLAY_PORT), "--bind", "127.0.0.1"],
        cwd=str(root),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    base = f"http://127.0.0.1:{REPLAY_PORT}"
    try:
        deadline = time.monotonic() + STARTUP_TIMEOUT_S
        while time.monotonic() < deadline:
            if fetch(f"{base}/")[0]:
                break
            time.sleep(0.3)
        else:  # pragma: no cover — pywb failed to start
            proc.terminate()
            raise AssertionError(f"pywb did not start:\n{proc.communicate(timeout=10)[0]}")

        target = f"{blog.rstrip('/')}/harris.html"
        code, body = fetch(f"{base}/imported/2026mp_/{target}")
        assert code == 200, f"replay from the wacz returned {code}"
        assert UNIQUE_PHRASE in body, "the export replayed, but not with our content in it"
    finally:
        proc.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.communicate(timeout=15)


def test_the_export_holds_every_capture_under_a_unique_name(
    authed: TestClient, settings: Settings, blog: str
) -> None:
    """Two captures, whose WARCs share every filename on disk.

    Both write `part-00000.warc.gz` and `part-meta.warc.gz`. A WACZ index
    names a file by basename alone, so packaged as they are, half the entries
    would resolve to the other capture's file — and both files exist and both
    parse, so nothing would say so.
    """
    site_id = capture_the_blog(authed, blog)
    second = authed.post(f"/api/sites/{site_id}/capture", json={"kind": "full"}, headers=XHR)
    assert second.status_code == 202, second.text
    assert wait_for_job(authed, second.json()["job_id"])["status"] == "ok"

    captures = authed.get(f"/api/sites/{site_id}/captures").json()
    assert len(captures) == 2

    path = wacz_of(authed, settings, site_id)
    with zipfile.ZipFile(path) as zf:
        archived = [n for n in zf.namelist() if n.startswith("archive/")]
    assert len(archived) == len(set(archived)) >= 4
    for capture in captures:
        assert any(capture["dir_name"] in name for name in archived)

    check = authed.get(f"/api/sites/{site_id}/exports/{path.name}/verify").json()
    assert check["ok"], check["problems"]
    assert check["records"] > len(POSTS)


# ── integrity ────────────────────────────────────────────────────────────


def test_verification_finds_a_flipped_byte_in_a_real_archive(
    authed: TestClient, settings: Settings, blog: str
) -> None:
    site_id = capture_the_blog(authed, blog)

    first = authed.post("/api/maintenance/verify", headers=XHR)
    assert first.status_code == 202, first.text
    assert wait_for_job(authed, first.json()["job_id"])["status"] == "ok"
    health = authed.get("/api/maintenance/integrity").json()
    assert health["last_run"]["ok"] is True
    assert health["verified"] == 1
    assert health["oldest_unverified"] is None

    site = authed.get(f"/api/sites/{site_id}").json()
    capture = authed.get(f"/api/sites/{site_id}/captures").json()[0]
    warc = next(
        (
            storage.site_dir(settings, site["archive_path"])
            / storage.CAPTURES_DIR
            / capture["dir_name"]
            / storage.WARC_DIR
        ).glob("*.warc.gz")
    )
    raw = bytearray(warc.read_bytes())
    raw[len(raw) // 2] ^= 0xFF
    warc.write_bytes(bytes(raw))

    again = authed.post("/api/maintenance/verify", headers=XHR)
    job = wait_for_job(authed, again.json()["job_id"])
    assert job["status"] == "ok", job
    assert job["progress"]["findings"] == 1

    health = authed.get("/api/maintenance/integrity").json()
    assert health["last_run"]["ok"] is False
    finding = health["last_run"]["findings"][0]
    assert finding["kind"] == "mismatch"
    assert finding["capture_dir"] == capture["dir_name"]
    assert warc.name in finding["file"]
    # Never repaired, only reported: the file is left exactly as found.
    assert warc.read_bytes() == bytes(raw)


def test_a_stale_index_is_reported_separately_from_damage(
    authed: TestClient, settings: Settings, blog: str
) -> None:
    """Replay 503s for a page still on disk, which reads as data loss."""
    site_id = capture_the_blog(authed, blog)
    site = authed.get(f"/api/sites/{site_id}").json()

    index = replay.index_path(settings, site["archive_path"])
    index.write_text(
        index.read_text(encoding="utf-8").replace("part-00000.warc.gz", "part-00042.warc.gz"),
        encoding="utf-8",
    )

    queued = authed.post("/api/maintenance/verify", headers=XHR)
    job = wait_for_job(authed, queued.json()["job_id"])
    assert job["status"] == "ok"
    findings = authed.get("/api/maintenance/integrity").json()["last_run"]["findings"]
    assert [f["kind"] for f in findings] == ["stale-index"]


def fetch(url: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")
    except (TimeoutError, urllib.error.URLError, OSError):
        return 0, ""
