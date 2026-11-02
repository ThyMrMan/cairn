"""A site that rate-limits with a disguised error page (docs/18, docs/19 0.5).

fictionmania.tv answers a blocked request by redirecting it to /err429.html and
serving that with 200. Two things went wrong because of it: wget kept crawling,
archiving the error page under thousands of story URLs, and replay then showed
the error page in place of those stories. A site's `rate_limit_page` fixes both.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session
from warcio.statusandheaders import StatusAndHeaders
from warcio.warcwriter import WARCWriter

from cairn.config import Settings
from cairn.db.models import Site
from cairn.engines.protocol import EventWriter, JobSpec
from cairn.engines.wget import Runner
from cairn.services import replay, storage

ERR = "https://www.fictionmania.tv/err429.html"
STORY = "https://www.fictionmania.tv/stories/readtextstory.html?storyID={}"
RULE = r"/err429\.html"


def response(
    writer: WARCWriter, url: str, status: str, headers: list[tuple[str, str]], body: bytes
) -> None:
    http = StatusAndHeaders(
        status, [*headers, ("Content-Length", str(len(body)))], protocol="HTTP/1.1"
    )
    writer.write_record(
        writer.create_warc_record(
            url, "response", payload=io.BytesIO(body), length=len(body), http_headers=http
        )
    )


def make_site(db: Session, settings: Settings) -> Site:
    site = Site(
        folder_id=1,
        slug="fm",
        title="fm",
        seed_url="https://www.fictionmania.tv/",
        primary_host="www.fictionmania.tv",
        archive_path="Unfiled/fm",
    )
    db.add(site)
    db.flush()
    storage.ensure_site_dirs(settings, site.archive_path)
    out = storage.ensure_capture_dirs(settings, site.archive_path, "20261030T130216Z-full-wget")
    with (out / "warc" / "part-00000.warc.gz").open("wb") as fh:
        writer = WARCWriter(fh, gzip=True)
        response(
            writer,
            STORY.format(1),
            "200 OK",
            [("Content-Type", "text/html")],
            b"<html>a real story</html>",
        )
        response(writer, STORY.format(2), "302 Found", [("Location", ERR)], b"")
        response(writer, STORY.format(3), "302 Found", [("Location", "/err429.html")], b"")
        response(writer, ERR, "200 OK", [("Content-Type", "text/html")], b"<html>slow down</html>")
        # An ordinary redirect elsewhere is left alone.
        response(
            writer,
            "https://www.fictionmania.tv/old",
            "301 Moved",
            [("Location", STORY.format(1))],
            b"",
        )
    return site


def indexed_urls(settings: Settings, site: Site) -> set[str]:
    lines = replay.index_path(settings, site.archive_path).read_text().splitlines()
    return {json.loads(line.split(" ", 2)[2])["url"] for line in lines}


def test_the_error_page_and_redirects_to_it_are_not_replayed(
    authed: Any, db: Session, settings: Settings
) -> None:
    site = make_site(db, settings)
    result = replay.build_index(settings, site.archive_path, withhold=replay.rate_limit_rules(RULE))
    assert indexed_urls(settings, site) == {STORY.format(1), "https://www.fictionmania.tv/old"}
    assert result.withheld == 3


def test_setting_the_page_on_a_site_withholds_it(
    authed: Any, db: Session, settings: Settings
) -> None:
    site = make_site(db, settings)
    assert not any("err429" in p for p in replay.withheld_patterns(db, site))
    site.scope_settings = {**(site.scope_settings or {}), "rate_limit_page": RULE}
    patterns = replay.withheld_patterns(db, site)
    assert RULE in patterns and f"{replay.REDIRECT_RULE}{RULE}" in patterns

    # Existing captures are cleaned by an ordinary update: the rule list is
    # part of the index state, so a change rebuilds it.
    replay.update_index(settings, site.archive_path, withhold=[])
    assert STORY.format(2) in indexed_urls(settings, site)
    replay.update_index(settings, site.archive_path, withhold=patterns)
    assert STORY.format(2) not in indexed_urls(settings, site)


def test_without_a_rule_nothing_extra_is_withheld(
    authed: Any, db: Session, settings: Settings
) -> None:
    site = make_site(db, settings)
    result = replay.build_index(settings, site.archive_path, withhold=[])
    assert result.withheld == 0
    assert len(indexed_urls(settings, site)) == 5


# ── the crawl stops ──────────────────────────────────────────────────────


class FakeWget:
    returncode = 0

    def communicate(self, timeout: float | None = None) -> tuple[bytes, bytes]:
        return b"", b""

    def poll(self) -> int:
        return 0

    def terminate(self) -> None:
        pass


def runner(tmp_path: Path, **config: Any) -> tuple[Runner, list[dict[str, Any]]]:
    events: list[dict[str, Any]] = []

    class Sink(io.StringIO):
        def write(self, s: str) -> int:
            events.extend(json.loads(line) for line in s.splitlines() if line.strip())
            return len(s)

    (tmp_path / "out" / "warc").mkdir(parents=True)
    spec = JobSpec(
        job_id=1,
        site={"id": 1, "slug": "fm", "title": "fm"},  # type: ignore[arg-type]
        output_dir=str(tmp_path / "out"),
        temp_dir=str(tmp_path / "tmp"),
        config=config,
    )
    return Runner(spec, EventWriter(Sink())), events


def test_wget_stops_once_requests_keep_landing_on_the_error_page(tmp_path: Path) -> None:
    run, events = runner(tmp_path, rate_limit_page=RULE)
    run._check_rate_limit(STORY.format(1), None)
    run._check_rate_limit(STORY.format(2), ERR)
    run._check_rate_limit(STORY.format(3), ERR)
    assert not run.rate_limited
    run._check_rate_limit(STORY.format(4), ERR)
    assert run.rate_limited and run.terminating
    assert [e["code"] for e in events if e["type"] == "warning"] == ["rate_limited"]

    run.proc = FakeWget()  # type: ignore[assignment]
    from cairn.engines.wget import Tailer

    out = tmp_path / "out"
    run._finish(Tailer(out / "warc" / "part.cdx"), Tailer(out / "crawl.log"), 0.0)
    (result,) = [e for e in events if e["type"] == "result"]
    assert result["status"] == "partial"
    assert result["stats"]["rate_limited"] is True
    assert "rate-limiting" in result["error"]


def test_without_a_rate_limit_page_nothing_stops(tmp_path: Path) -> None:
    run, _events = runner(tmp_path)
    for n in range(10):
        run._check_rate_limit(STORY.format(n), ERR)
    assert not run.rate_limited
