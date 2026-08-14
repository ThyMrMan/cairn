"""M3's exit criterion, as a test.

Capture a site for real, then serve it back through a real pywb and read the
page out of the iframe's own URL. Everything between — the CDXJ, the
site-relative filenames, the collection tree, the timestamp routing — is
exercised by getting the right bytes back rather than by inspecting the parts.

Needs GNU wget and pywb, so it runs in the container and in CI. Skipped
elsewhere for the same MAX_PATH reason as the capture e2e.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from cairn.config import Settings
from cairn.services import replay
from tests.conftest import XHR
from tests.test_capture_e2e import run_capture, wait_for_job

pytestmark = [
    pytest.mark.skipif(shutil.which("wget") is None, reason="needs GNU wget on PATH"),
    pytest.mark.skipif(shutil.which("wayback") is None, reason="needs pywb on PATH"),
    pytest.mark.skipif(
        os.name == "nt", reason="mingw wget hits MAX_PATH under pytest tmp_path; runs in Docker/CI"
    ),
]

REPLAY_PORT = 8897
STARTUP_TIMEOUT_S = 30


@pytest.fixture
def pywb(settings: Settings) -> Iterator[str]:
    """A real pywb over the same data directory the app just wrote to."""
    replay.write_config(settings)
    proc = subprocess.Popen(
        ["wayback", "--port", str(REPLAY_PORT), "--bind", "127.0.0.1"],
        cwd=str(settings.replay_dir),
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
        yield base
    finally:
        proc.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.communicate(timeout=15)


def fetch(url: str) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(url, timeout=15) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except OSError:
        return 0, b""


def test_an_archived_page_replays_with_its_real_content(
    authed: TestClient, settings: Settings, site_server: str, pywb: str
) -> None:
    """The payoff: the page comes back out of the WARC, not off the internet."""
    _job, site_id = run_capture(authed, site_server)

    status = authed.get(f"/api/sites/{site_id}/replay", headers=XHR).json()
    assert status["records"] > 0, "the capture did not produce an index"

    versions = authed.get(
        f"/api/sites/{site_id}/replay/versions?url={site_server}post-1.html", headers=XHR
    ).json()
    assert versions["count"] >= 1
    timestamp = versions["versions"][-1]["timestamp"]

    # `mp_` asks pywb for the content itself rather than the framing wrapper.
    code, body = fetch(f"{pywb}/{status['collection']}/{timestamp}mp_/{site_server}post-1.html")

    assert code == 200, f"replay returned {code}"
    assert b"UNIQUE-CONTENT-MARKER-ONE" in body


def test_subresources_are_rewritten_into_the_archive(
    authed: TestClient, settings: Settings, site_server: str, pywb: str
) -> None:
    """No live leak. The index page's `<img src="/logo.png">` must come back
    pointing at the collection, not at the origin server — a replay that
    silently fetches from the live internet is both a privacy leak and a lie
    about what the archive contains (docs/07)."""
    _job, site_id = run_capture(authed, site_server)
    status = authed.get(f"/api/sites/{site_id}/replay", headers=XHR).json()
    collection = status["collection"]

    versions = authed.get(
        f"/api/sites/{site_id}/replay/versions?url={site_server}", headers=XHR
    ).json()
    timestamp = versions["versions"][-1]["timestamp"]

    code, body = fetch(f"{pywb}/{collection}/{timestamp}mp_/{site_server}")

    assert code == 200
    assert b"logo.png" in body, "the image reference vanished entirely"
    assert f"/{collection}/".encode() in body, "the page still points at the live server"

    # And the rewritten image really is served from the archive.
    image_code, image = fetch(f"{pywb}/{collection}/{timestamp}im_/{site_server}logo.png")
    assert image_code == 200
    assert image.startswith(b"\x89PNG")


def test_the_framed_wrapper_is_what_the_iframe_loads(
    authed: TestClient, settings: Settings, site_server: str, pywb: str
) -> None:
    _job, site_id = run_capture(authed, site_server)
    status = authed.get(f"/api/sites/{site_id}/replay", headers=XHR).json()

    code, body = fetch(f"{pywb}/{status['collection']}/{site_server}")

    assert code == 200
    assert b"iframe" in body.lower()


def test_two_captures_of_the_same_page_are_both_reachable(
    authed: TestClient, settings: Settings, site_server: str, pywb: str
) -> None:
    """The half of the exit criterion that the index decides.

    Both captures write `warc/part-00000.warc.gz`, so this is the case that
    breaks — with a 503, not a wrong answer — if the index records basenames.
    """
    _job, site_id = run_capture(authed, site_server)
    second = authed.post(f"/api/sites/{site_id}/capture", json={"kind": "full"}, headers=XHR)
    wait_for_job(authed, second.json()["job_id"])

    status = authed.get(f"/api/sites/{site_id}/replay", headers=XHR).json()
    versions = authed.get(
        f"/api/sites/{site_id}/replay/versions?url={site_server}post-1.html", headers=XHR
    ).json()
    assert versions["count"] == 2, f"expected two captures, got {versions['count']}"

    for version in versions["versions"]:
        url = f"{pywb}/{status['collection']}/{version['timestamp']}mp_/{site_server}post-1.html"
        code, body = fetch(url)
        assert code == 200, f"{version['timestamp']} returned {code}"
        assert b"UNIQUE-CONTENT-MARKER-ONE" in body


def test_the_raw_record_is_served_by_the_app_as_an_attachment(
    authed: TestClient, settings: Settings, site_server: str
) -> None:
    """Never rendered inline on the app origin — that would reintroduce the
    cross-origin scripting the separate replay port exists to prevent."""
    _job, site_id = run_capture(authed, site_server)

    meta = authed.get(
        f"/api/sites/{site_id}/replay/record?url={site_server}post-1.html", headers=XHR
    )
    assert meta.status_code == 200, meta.text
    assert meta.json()["http_status"] == "200"

    raw = authed.get(
        f"/api/sites/{site_id}/replay/record?url={site_server}post-1.html&download=true",
        headers=XHR,
    )
    assert raw.status_code == 200
    assert raw.headers["content-type"] == "application/octet-stream"
    assert raw.headers["content-disposition"].startswith("attachment")
    assert raw.headers["x-content-type-options"] == "nosniff"
    assert b"UNIQUE-CONTENT-MARKER-ONE" in raw.content


def test_the_bare_form_stands_alone_without_javascript(
    authed: TestClient, settings: Settings, site_server: str, pywb: str
) -> None:
    """The `mp_` form is what a no-scripts view has to be built on.

    Reported as a blog whose archived page hangs the browser — and identically
    through pywb on its own, which places the fault in the page's scripts
    rather than anywhere here. The replay panel can decline to run them by
    dropping `allow-scripts` from the iframe sandbox, but only against `mp_`:
    the framed URL *is* a script, a wrapper whose whole job is done in
    JavaScript, so blocking scripts and loading it renders nothing at all.
    That was shipped and had to be corrected.

    What makes `mp_` viable is that pywb rewrites subresources server-side, so
    stylesheets and images still resolve with no JavaScript involved. Asserted
    here rather than trusted, because it is the property the feature rests on
    and nothing else in the suite would notice it changing.
    """
    _job, site_id = run_capture(authed, site_server)
    status = authed.get(f"/api/sites/{site_id}/replay", headers=XHR).json()
    collection = status["collection"]

    versions = authed.get(
        f"/api/sites/{site_id}/replay/versions?url={site_server}", headers=XHR
    ).json()
    timestamp = versions["versions"][-1]["timestamp"]

    code, body = fetch(f"{pywb}/{collection}/{timestamp}mp_/{site_server}")
    assert code == 200
    text = body.decode("utf-8", "replace")

    # Server-side rewriting: the modifiers pywb puts on subresources. `im_` for
    # images and `cs_` for stylesheets are what a script-free page follows.
    assert f"/{collection}/{timestamp}im_/" in text, "images were not rewritten server-side"

    for modifier, marker in ((f"{timestamp}im_/", "logo.png"),):
        target = next(
            (
                part
                for part in re.findall(r'src="([^"]+)"', text)
                if modifier in part and marker in part
            ),
            None,
        )
        assert target, f"no {marker} rewritten with {modifier} in the page"
        absolute = target if target.startswith("http") else f"{pywb}{target}"
        sub_code, sub_body = fetch(absolute)
        assert sub_code == 200, f"{absolute} returned {sub_code}"
        assert sub_body, "the archived subresource came back empty"
