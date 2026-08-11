"""M7's exit criterion, run for real.

> A JS-heavy site with lazy-loaded images that wget captures badly is captured
> correctly by browsertrix, selected per-site in the UI, with both engines'
> captures replaying from the same collection.

Both halves are asserted against the thing itself: a real wget and a real
browsertrix container crawl the same fixture, the WARCs are read back with
warcio, and the site's single CDXJ index is checked for records from both.

Needs the Docker socket and pulls a ~1 GB image, so it skips unless both are
there — the same arrangement as the wget and Chromium suites, one step further
out.
"""

from __future__ import annotations

import asyncio
import shutil
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from cairn.config import Settings
from cairn.db.models import Capture, CaptureUrl
from cairn.services import containers
from tests.conftest import LAZY_NAMES, XHR


def _docker_ready() -> bool:
    """Both a reachable daemon and an explicit opt-in.

    The socket alone is not enough to run these: they pull most of a gigabyte
    and take minutes, and a CI runner has a socket. So the second gate is a
    variable somebody sets deliberately — CI runs them on a schedule rather
    than on every push.
    """
    import os

    if os.environ.get("CAIRN_TEST_CONTAINERS", "").lower() not in ("1", "true", "yes"):
        return False
    if not containers.available()[0]:
        return False

    async def ping() -> bool:
        async with containers.client(timeout=5.0) as http:
            return (await http.get("/version")).status_code == 200

    try:
        return asyncio.run(ping())
    except Exception:
        return False


needs_wget = pytest.mark.skipif(shutil.which("wget") is None, reason="needs wget")
needs_docker = pytest.mark.skipif(
    not _docker_ready(), reason="needs the Docker socket and CAIRN_TEST_CONTAINERS=1"
)

# The fixture server runs in this process, so the engine container has to be
# on a network that can reach it. Set by the harness that runs these.
NETWORK = "cairn-probe-net"


def _wait(client: TestClient, job_id: int, *, timeout: float = 900.0) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}", headers=XHR).json()
        if job["status"] in ("ok", "failed", "cancelled", "interrupted"):
            return job
        time.sleep(1.0)
    raise AssertionError(f"job {job_id} never finished")


def _archived(settings: Settings, archive_path: str, dir_name: str) -> set[str]:
    """Every URL with a response record in this capture's WARCs."""
    from warcio.archiveiterator import ArchiveIterator

    warc_dir = settings.archives_dir / archive_path / "captures" / dir_name / "warc"
    found: set[str] = set()
    for warc in sorted(warc_dir.glob("*.warc.gz")):
        with warc.open("rb") as handle:
            for record in ArchiveIterator(handle):
                if record.rec_type == "response":
                    found.add(record.rec_headers.get_header("WARC-Target-URI") or "")
    return found


def _capture_with(client: TestClient, site_id: int, engine_id: str, config: dict) -> int:
    res = client.patch(
        f"/api/sites/{site_id}",
        json={"engine_id": engine_id, "engine_config": config},
        headers=XHR,
    )
    assert res.status_code == 200, res.text
    job = client.post(f"/api/sites/{site_id}/capture", json={"kind": "full"}, headers=XHR).json()
    return int(job["job_id"])


@needs_wget
@needs_docker
def test_a_js_heavy_site_needs_the_second_engine(
    authed: TestClient, db: Session, settings: Settings, js_server: str
) -> None:
    site = authed.post(
        "/api/sites", json={"seed_url": js_server, "title": "Gallery"}, headers=XHR
    ).json()
    site_id = site["id"]

    # ── wget first: the control, and the reason this milestone exists
    wget_job = _capture_with(authed, site_id, "wget-warc", {})
    assert _wait(authed, wget_job)["status"] == "ok"

    # ── then browsertrix, on the same site
    btx_job = _capture_with(
        authed,
        site_id,
        "browsertrix",
        {"docker_network": NETWORK, "post_load_delay_s": 2, "workers": 1},
    )
    outcome = _wait(authed, btx_job)
    assert outcome["status"] == "ok", outcome.get("error")

    db.expire_all()
    captures = db.scalars(
        select(Capture).where(Capture.site_id == site_id).order_by(Capture.started_at)
    ).all()
    assert [c.engine_id for c in captures] == ["wget-warc", "browsertrix"]
    wget_capture, btx_capture = captures

    wget_urls = _archived(settings, site["archive_path"], wget_capture.dir_name)
    btx_urls = _archived(settings, site["archive_path"], btx_capture.dir_name)

    # ── the lazy images: three in the browser, none for wget
    def images(urls: set[str]) -> set[str]:
        return {u for u in urls if "/img/" in u}

    assert images(wget_urls) == set(), (
        "the images have no src in the HTML, so wget cannot know they exist: "
        f"{sorted(images(wget_urls))}"
    )
    assert len(images(btx_urls)) == len(LAZY_NAMES), sorted(images(btx_urls))

    # ── the script-generated link
    assert not any("post.html" in u for u in wget_urls), (
        "the link is added by script, so it is not in the HTML wget receives"
    )
    assert any("post.html" in u for u in btx_urls)

    # ── both are the same site, so both are in the same replay collection
    index = settings.archives_dir / site["archive_path"] / "index" / "site.cdxj"
    assert index.is_file(), "the cdxj-index post-processor runs for every engine"
    lines = index.read_text(encoding="utf-8").splitlines()
    assert any("post.html" in line for line in lines), "browsertrix's records are indexed"
    indexed_warcs = {
        line.split('"filename": "', 1)[1].split('"', 1)[0] for line in lines if '"filename"' in line
    }
    assert any(wget_capture.dir_name in name for name in indexed_warcs), (
        f"wget's WARCs are missing from the index: {sorted(indexed_warcs)[:4]}"
    )
    assert any(btx_capture.dir_name in name for name in indexed_warcs), (
        f"browsertrix's WARCs are missing from the index: {sorted(indexed_warcs)[:4]}"
    )

    # ── and the URL list reflects what each actually fetched
    btx_rows = db.scalars(
        select(CaptureUrl.url).where(CaptureUrl.capture_id == btx_capture.id)
    ).all()
    assert any("/img/" in url for url in btx_rows), (
        "browsertrix's url events come from its own CDXJ, not its logs"
    )


@needs_docker
def test_a_cancelled_container_capture_stops_the_container(
    authed: TestClient, db: Session, js_server: str
) -> None:
    """A sibling container does not die with its parent, so cancellation has
    to reach it explicitly — otherwise a cancelled capture keeps crawling."""
    supervisor = authed.app.state.supervisor  # type: ignore[attr-defined]
    site = authed.post(
        "/api/sites", json={"seed_url": js_server, "title": "Gallery"}, headers=XHR
    ).json()

    job_id = _capture_with(
        authed,
        site["id"],
        "browsertrix",
        {"docker_network": NETWORK, "page_extra_delay_s": 30},
    )
    # Let it get as far as starting the container.
    time.sleep(20)
    asyncio.run(supervisor.cancel(job_id))
    outcome = _wait(authed, job_id, timeout=300)

    assert outcome["status"] in ("cancelled", "ok"), outcome
    assert not _our_containers_running(), "the engine container outlived its job"


def _our_containers_running() -> bool:
    import json as _json

    async def check() -> bool:
        async with containers.client() as http:
            params = {
                "filters": _json.dumps({"label": [f"{containers.LABEL_MANAGED}=true"]}),
            }
            response = await http.get("/containers/json", params=params)
            return bool(response.json())

    return asyncio.run(check())


@needs_docker
def test_orphaned_engine_containers_are_swept_at_boot() -> None:
    """Our process can be killed mid-capture; the crawler keeps going, holding
    the archive open and writing into a capture already marked interrupted."""

    async def scenario() -> int:
        async with containers.client() as http:
            spec = containers.RunSpec(image="busybox:latest", argv=["sleep", "300"], job_id=999)
            container = await containers.create(http, spec)
            await containers.start(http, container)
            try:
                return await containers.sweep(http)
            finally:
                await containers.remove(http, container)

    assert asyncio.run(scenario()) >= 1
    assert not _our_containers_running()


def test_the_capture_directory_layout_is_the_same_whichever_engine_ran(
    settings: Settings,
) -> None:
    """Replay finds WARCs by globbing `captures/*/warc/*.warc.gz`, so an
    engine writing its own layout produces an archive nothing can serve.
    browsertrix writes `collections/<name>/archive/` and its adapter moves the
    files; this guards the shape that made that necessary."""
    from cairn.services import replay, storage

    assert storage.WARC_DIR == "warc"
    assert storage.CAPTURES_DIR == "captures"
    assert replay.site_warcs(settings, "nothing-here") == []
