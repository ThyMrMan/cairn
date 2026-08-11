"""M5's exit criterion, as a test.

*Upload a Tampermonkey script, the tool mints a working cookie jar from it,
and a capture using that profile returns real content.*

Every step is the real one: a real Chromium runs the script, a real wget runs
the crawl with the jar the script produced, and the assertion reads the page
text back out of the WARC. Nothing here touches a real site or a real account
— the fixture gates everything behind a cookie its own button sets, which is
the shape of the Blogger interstitial this feature exists for.

Needs wget and Chromium, so it runs in the container and in CI.
"""

from __future__ import annotations

import gzip
import os
import shutil

import pytest
from fastapi.testclient import TestClient

from cairn.config import Settings
from cairn.services import browser
from tests.conftest import GATE_COOKIE, XHR
from tests.test_capture_e2e import wait_for_job

pytestmark = [
    pytest.mark.skipif(shutil.which("wget") is None, reason="needs GNU wget on PATH"),
    pytest.mark.skipif(not browser.availability()[0], reason="needs Playwright and Chromium"),
    pytest.mark.skipif(
        os.name == "nt", reason="mingw wget hits MAX_PATH under pytest tmp_path; runs in Docker/CI"
    ),
]

DISMISSER = """
// ==UserScript==
// @name         Dismiss the content warning
// @match        <URL>*
// @grant        GM_setValue
// @run-at       document-start
// ==/UserScript==
GM_setValue('cairn-ran', true);
document.addEventListener('DOMContentLoaded', function () {
  var button = document.getElementById('continue');
  if (button) button.click();
});
"""


def warc_text(settings: Settings, archive_path: str) -> str:
    """Everything archived, as one string. Reading the actual bytes back is
    the only assertion that means anything here."""
    root = settings.archives_dir / archive_path / "captures"
    out = []
    for warc in sorted(root.glob("*/warc/*.warc.gz")):
        with gzip.open(warc, "rb") as fh:
            out.append(fh.read().decode("utf-8", "replace"))
    return "".join(out)


def test_a_userscript_mints_a_jar_that_captures_real_content(
    authed: TestClient, settings: Settings, gated_server: str
) -> None:
    # 1. A profile with a userscript, pointed at the gated site.
    profile = authed.post(
        "/api/profiles",
        json={"name": "gate", "mode": "userscript", "verify_url": gated_server},
        headers=XHR,
    ).json()

    uploaded = authed.put(
        f"/api/profiles/{profile['id']}/script",
        files={"file": ("dismiss.user.js", DISMISSER.replace("<URL>", gated_server), "text/plain")},
        headers=XHR,
    )
    assert uploaded.status_code == 200, uploaded.text
    assert uploaded.json()["script"]["name"] == "Dismiss the content warning"

    # 2. Mint: run it in a real browser and keep what it earns.
    minted = authed.post(f"/api/profiles/{profile['id']}/mint", headers=XHR)
    assert minted.status_code == 200, minted.text
    result = minted.json()["result"]
    assert result["ok"], result["reason"]
    assert minted.json()["profile"]["cookie_count"] >= 1

    # 3. The jar gets past the gate, checked the way wget will see it.
    verified = authed.post(f"/api/profiles/{profile['id']}/verify", headers=XHR).json()
    assert verified["ok"], verified["reason"]

    # 4. Capture with that profile, and read the archive back.
    site = authed.post(
        "/api/sites",
        json={"seed_url": gated_server, "title": "Gated", "profile_id": profile["id"]},
        headers=XHR,
    ).json()
    job = authed.post(f"/api/sites/{site['id']}/capture", json={"kind": "full"}, headers=XHR)
    assert job.status_code == 202, job.text
    wait_for_job(authed, job.json()["job_id"])

    archived = warc_text(settings, site["archive_path"])
    assert "UNIQUE-GATED-INDEX" in archived, "the capture archived the interstitial, not the blog"
    assert "I understand and wish to continue" not in archived


def test_capturing_without_the_profile_archives_the_interstitial(
    authed: TestClient, settings: Settings, gated_server: str
) -> None:
    """The control, and the failure the whole feature exists to prevent.

    Without a jar the crawl succeeds, writes a WARC, and reports no errors —
    every page inside it is the content warning. So the gap report has to say
    so, and the capture must not be left claiming it went fine.
    """
    site = authed.post(
        "/api/sites", json={"seed_url": gated_server, "title": "Ungated"}, headers=XHR
    ).json()
    job = authed.post(f"/api/sites/{site['id']}/capture", json={"kind": "full"}, headers=XHR)
    wait_for_job(authed, job.json()["job_id"])

    archived = warc_text(settings, site["archive_path"])
    assert "I understand and wish to continue" in archived
    assert "UNIQUE-GATED-INDEX" not in archived

    captures = authed.get(f"/api/sites/{site['id']}/captures", headers=XHR).json()
    detail = authed.get(f"/api/captures/{captures[0]['id']}", headers=XHR).json()
    warnings = (detail.get("manifest") or {}).get("stats", {}).get("warnings", [])

    assert any("content warning" in w for w in warnings), warnings
    assert captures[0]["status"] == "partial", (
        "a capture full of interstitials must not report success"
    )


def test_an_expiring_jar_is_re_minted_before_the_capture_runs(
    authed: TestClient, settings: Settings, gated_server: str
) -> None:
    """docs/06 wanted a running capture paused and resumed with a fresh jar.
    wget reads --load-cookies once at startup, so the only place this can do
    any good is before the job begins."""
    from datetime import datetime, timedelta

    from cairn.db.models import AccessProfile
    from cairn.db.types import utcnow

    profile = authed.post(
        "/api/profiles",
        json={"name": "aging", "mode": "userscript", "verify_url": gated_server},
        headers=XHR,
    ).json()
    authed.put(
        f"/api/profiles/{profile['id']}/script",
        files={"file": ("d.user.js", DISMISSER.replace("<URL>", gated_server), "text/plain")},
        headers=XHR,
    )
    authed.post(f"/api/profiles/{profile['id']}/mint", headers=XHR)

    # Age it past the window without touching the cookies themselves.
    aged = utcnow() - timedelta(days=30)
    factory = authed.app.state.sessionmaker  # type: ignore[attr-defined]
    with factory() as session:
        row = session.get(AccessProfile, profile["id"])
        assert row is not None
        row.minted_at = aged
        session.commit()

    site = authed.post(
        "/api/sites",
        json={"seed_url": gated_server, "title": "Aging", "profile_id": profile["id"]},
        headers=XHR,
    ).json()
    job = authed.post(f"/api/sites/{site['id']}/capture", json={"kind": "full"}, headers=XHR)
    wait_for_job(authed, job.json()["job_id"])

    refreshed = authed.get(f"/api/profiles/{profile['id']}", headers=XHR).json()
    assert refreshed["minted_at"] is not None
    minted_at = datetime.fromisoformat(str(refreshed["minted_at"]).replace("Z", "+00:00"))
    assert minted_at > aged + timedelta(days=1), (
        f"the aged jar was not re-minted before the crawl (still {minted_at})"
    )

    # And the fresh jar is the one the crawl actually used.
    archived = warc_text(settings, site["archive_path"])
    assert "UNIQUE-GATED-INDEX" in archived


def test_the_jar_ends_up_inside_the_warc(
    authed: TestClient, settings: Settings, gated_server: str
) -> None:
    """Pinned because it is security-relevant and cannot be fixed from here.

    A WARC records the *request* as well as the response, and the request
    carries the `Cookie:` header wget sent. So an archive of a gated site
    contains the credential that opened the gate. For a content-warning
    consent that is uninteresting; for an interactive profile holding a login
    it is a real one, and it travels with any WACZ export or shared file.

    Nothing in wget can suppress it, and rewriting finished WARCs to redact
    would invalidate the checksums the integrity job depends on. So it is
    documented (docs/11) and warned about at capture time instead — and
    asserted here so nobody later assumes otherwise.
    """
    profile = authed.post(
        "/api/profiles",
        json={"name": "leaky", "mode": "userscript", "verify_url": gated_server},
        headers=XHR,
    ).json()
    authed.put(
        f"/api/profiles/{profile['id']}/script",
        files={"file": ("d.user.js", DISMISSER.replace("<URL>", gated_server), "text/plain")},
        headers=XHR,
    )
    authed.post(f"/api/profiles/{profile['id']}/mint", headers=XHR)

    site = authed.post(
        "/api/sites",
        json={"seed_url": gated_server, "title": "Leaky", "profile_id": profile["id"]},
        headers=XHR,
    ).json()
    job = authed.post(f"/api/sites/{site['id']}/capture", json={"kind": "full"}, headers=XHR)
    wait_for_job(authed, job.json()["job_id"])

    assert f"Cookie: {GATE_COOKIE}" in warc_text(settings, site["archive_path"])
