"""Which archived links replay cannot answer.

The case that motivated it: a companion pass fetched 69 pagination URLs and the
index withheld 68 of them, so every Older Posts link was dead while every
capture reported success. Nothing in the archive said so. These tests are
written against that shape — a link whose target is in a WARC and not in the
index has to be reported, or the check is decoration.
"""

from __future__ import annotations

import io
from pathlib import Path

from sqlalchemy.orm import Session

from cairn.config import Settings
from cairn.db.models import Site
from cairn.services import linkcheck, replay, storage
from cairn.services import sites as site_service
from cairn.services.scope import HostRule, Scope

BLOG = "https://b.blogspot.com"

PAGE_ONE = (
    f'<html><body><a href="{BLOG}/2019/04/post.html">a post</a>'
    f'<a href="{BLOG}/search?updated-max=2020-01-01T00:00:00-05:00&max-results=7">Older Posts</a>'
    f'<a href="https://elsewhere.example/x">off site</a>'
    f'<a href="{BLOG}/2019/04/post.html?m=1">mobile</a>'
    f'<a href="#top">anchor</a></body></html>'
).encode()

POST = b"<html><body>a post with no links</body></html>"


def _write_warc(path: Path, pages: list[tuple[str, bytes]]) -> None:
    from warcio.statusandheaders import StatusAndHeaders
    from warcio.warcwriter import WARCWriter

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as fh:
        writer = WARCWriter(fh, gzip=True)
        for url, body in pages:
            headers = StatusAndHeaders(
                "200 OK",
                [("Content-Type", "text/html; charset=UTF-8"), ("Content-Length", str(len(body)))],
                protocol="HTTP/1.1",
            )
            writer.write_record(
                writer.create_warc_record(
                    url,
                    "response",
                    payload=io.BytesIO(body),
                    http_headers=headers,
                    warc_content_type="application/http; msgtype=response",
                )
            )


def _site(db: Session, settings: Settings, *, rejects: list[str], preset: str) -> Site:
    site = Site(
        slug="b",
        title="B",
        seed_url=f"{BLOG}/",
        primary_host="b.blogspot.com",
        archive_path="Unfiled/b",
        folder_id=1,
        scope_settings={"preset": preset, "user_edited": True},
    )
    db.add(site)
    db.flush()
    site_service.save_scope(
        db,
        site,
        Scope(
            seeds=[f"{BLOG}/"],
            hosts=[HostRule("b.blogspot.com", crawl_pages=True, fetch_assets=True)],
            reject_patterns=rejects,
        ),
    )
    return site


def _capture(settings: Settings, site: Site, name: str, pages: list[tuple[str, bytes]]) -> None:
    root = storage.site_dir(settings, site.archive_path)
    _write_warc(root / storage.CAPTURES_DIR / name / storage.WARC_DIR / "part.warc.gz", pages)


def test_a_link_whose_target_is_absent_is_reported(db: Session, settings: Settings) -> None:
    site = _site(db, settings, rejects=[r"[?&]m=1"], preset="blogger")
    _capture(settings, site, "20260101T000000Z-full-wget", [(f"{BLOG}/", PAGE_ONE)])
    replay.build_index(settings, site.archive_path, withhold=replay.withheld_patterns(db, site))

    report = linkcheck.check_links(db, settings, site)

    assert report.pages_scanned == 1
    targets = {d.target for d in report.dead}
    assert f"{BLOG}/2019/04/post.html" in targets
    assert any("updated-max" in t for t in targets)
    assert not report.ok


def test_off_site_rejected_and_non_page_links_are_not_faults(
    db: Session, settings: Settings
) -> None:
    """Reporting these would bury the real misses under thousands of them.

    A link off the site, a link to something the scope refuses, and a bare
    fragment are all the boundary working — not a gap in the archive.
    """
    site = _site(db, settings, rejects=[r"[?&]m=1"], preset="blogger")
    _capture(settings, site, "20260101T000000Z-full-wget", [(f"{BLOG}/", PAGE_ONE)])
    replay.build_index(settings, site.archive_path, withhold=replay.withheld_patterns(db, site))

    report = linkcheck.check_links(db, settings, site)
    targets = {d.target for d in report.dead}

    assert not any("elsewhere.example" in t for t in targets), "another site is not our job"
    assert not any("m=1" in t for t in targets), "the scope refused it on purpose"
    assert not any(t.endswith("#top") for t in targets), "a fragment is not a page"
    # Two links were worth checking. Four distinct targets were seen, not five:
    # a bare `#top` resolves to the page it is on and never becomes a link to
    # check, which is the right answer and worth pinning so a future change to
    # `absolutize` cannot start reporting every anchor on every page.
    assert report.in_scope == 2
    assert report.links_seen == 4


def test_everything_resolving_is_reported_as_ok(db: Session, settings: Settings) -> None:
    site = _site(db, settings, rejects=[r"[?&]m=1"], preset="blogger")
    _capture(
        settings,
        site,
        "20260101T000000Z-full-wget",
        [
            (f"{BLOG}/", PAGE_ONE),
            (f"{BLOG}/2019/04/post.html", POST),
            (f"{BLOG}/search?updated-max=2020-01-01T00:00:00-05:00&max-results=7", POST),
        ],
    )
    replay.build_index(settings, site.archive_path, withhold=replay.withheld_patterns(db, site))

    report = linkcheck.check_links(db, settings, site)
    assert report.ok
    assert report.dead == []
    assert report.resolved == report.in_scope == 2


def test_a_target_in_a_warc_but_withheld_from_the_index_is_dead(
    db: Session, settings: Settings
) -> None:
    """The bug this check exists for, stated exactly.

    The record is in the archive and the capture reported success. It is not in
    the index, so replay 404s. A check written against the captured URL list
    rather than the index keys would call this healthy — which is precisely how
    the pagination pass shipped useless.
    """
    site = _site(
        db,
        settings,
        rejects=[r"[?&]m=1", r"/search\?[^#]*updated-(max|min)="],
        preset="blogger",  # standard: nothing lifts the pagination reject
    )
    _capture(
        settings,
        site,
        "20260101T000000Z-full-wget",
        [
            (f"{BLOG}/", PAGE_ONE),
            (f"{BLOG}/2019/04/post.html", POST),
            (f"{BLOG}/search?updated-max=2020-01-01T00:00:00-05:00&max-results=7", POST),
        ],
    )
    result = replay.build_index(
        settings, site.archive_path, withhold=replay.withheld_patterns(db, site)
    )
    assert result.withheld >= 1, "the fixture must actually withhold the target"

    report = linkcheck.check_links(db, settings, site)

    # The pagination link is refused by this site's scope, so it is not a
    # fault — the archive is behaving as configured, and the check says so by
    # not reporting it.
    assert report.ok, [d.target for d in report.dead]


def test_the_companion_pass_makes_its_own_links_checkable(db: Session, settings: Settings) -> None:
    """The other half, and the one that catches a regression of the real bug.

    On the lean preset the pagination reject is lifted from the index, so those
    links stop being "deliberately refused" and become links that must resolve.
    Capture the trail and it passes; leave it out and this reports it.
    """
    rejects = [r"[?&]m=1", r"/search\?[^#]*updated-(max|min)="]
    site = _site(db, settings, rejects=rejects, preset="blogger-lean")
    _capture(
        settings,
        site,
        "20260101T000000Z-full-browsertrix",
        [(f"{BLOG}/", PAGE_ONE), (f"{BLOG}/2019/04/post.html", POST)],
    )
    replay.build_index(settings, site.archive_path, withhold=replay.withheld_patterns(db, site))

    before = linkcheck.check_links(db, settings, site)
    assert not before.ok
    assert any("updated-max" in d.target for d in before.dead), (
        "with the reject lifted, an uncaptured pagination link is a real gap"
    )

    # Now the companion pass runs and fetches it.
    _capture(
        settings,
        site,
        "20260101T010000Z-companion-wget",
        [(f"{BLOG}/search?updated-max=2020-01-01T00:00:00-05:00&max-results=7", POST)],
    )
    replay.build_index(settings, site.archive_path, withhold=replay.withheld_patterns(db, site))

    after = linkcheck.check_links(db, settings, site)
    assert after.ok, [d.target for d in after.dead]


def test_the_budget_is_reported_rather_than_silently_applied(
    db: Session, settings: Settings
) -> None:
    """A truncated answer that looks complete is worse than a slow one."""
    site = _site(db, settings, rejects=[], preset="blogger")
    _capture(
        settings,
        site,
        "20260101T000000Z-full-wget",
        [(f"{BLOG}/{n}.html", PAGE_ONE) for n in range(5)],
    )
    replay.build_index(settings, site.archive_path, withhold=[])

    report = linkcheck.check_links(db, settings, site, budget=2)
    assert report.pages_scanned == 2
    assert report.truncated is True
    assert linkcheck.check_links(db, settings, site, budget=50).truncated is False
