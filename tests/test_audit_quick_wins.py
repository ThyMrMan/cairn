"""Regression tests for the October 2026 audit's quick wins (docs/18).

Each test names the finding it pins down, so a failure points straight at
the write-up that explains why the behaviour matters.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import timedelta

import pytest
from sqlalchemy.orm import Session

from cairn.config import Settings
from cairn.db.models import AccessProfile, Capture, Discovery, Feed, Folder, Site
from cairn.db.types import utcnow
from cairn.discovery import sources
from cairn.discovery.fetch import Fetched
from cairn.discovery.platform import PRESETS
from cairn.services import discovery_service, search, storage, textextract
from cairn.services import sites as site_service
from cairn.services.scope import Scope
from tests.test_search import blogger_page, write_warc

# ── #3: every discovered URL reaches the crawler, not the first 200 ──────


def test_a_capture_is_seeded_with_every_discovered_url(db: Session, settings: Settings) -> None:
    site = site_service.create_site(db, settings, seed_url="https://blog.example.com/")
    urls = [f"https://blog.example.com/2026/{n:04d}.html" for n in range(1, 1201)]
    discovery = Discovery(
        site_id=site.id,
        started_at=utcnow(),
        finished_at=utcnow(),
        pages_fetched=1,
        urls_found=len(urls),
        summary={"sample_urls": urls[:200]},
    )
    db.add(discovery)
    db.flush()
    discovery_service.write_discovered_urls(settings, site, discovery.id, urls)

    scope = site_service.resolved_scope(db, site)
    seeds, counts = discovery_service.seeds_for_capture(db, site, scope, settings)
    assert counts["sitemap"] == 1200
    assert urls[-1] in seeds


def test_a_stale_url_list_falls_back_to_the_sample(db: Session, settings: Settings) -> None:
    """A list written by an older discovery must not seed a newer one."""
    site = site_service.create_site(db, settings, seed_url="https://blog.example.com/")
    discovery = Discovery(
        site_id=site.id,
        started_at=utcnow(),
        finished_at=utcnow(),
        pages_fetched=1,
        urls_found=1,
        summary={"sample_urls": ["https://blog.example.com/new.html"]},
    )
    db.add(discovery)
    db.flush()
    # Tagged with a different discovery's id: written by an older run.
    discovery_service.write_discovered_urls(
        settings, site, discovery.id + 1000, ["https://blog.example.com/old.html"]
    )

    scope = site_service.resolved_scope(db, site)
    seeds, _counts = discovery_service.seeds_for_capture(db, site, scope, settings)
    assert "https://blog.example.com/new.html" in seeds
    assert "https://blog.example.com/old.html" not in seeds


# ── #6: a sitemap walk that was cut short says so ────────────────────────


def _urlset(*locs: str) -> bytes:
    body = "".join(f"<url><loc>{loc}</loc></url>" for loc in locs)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{body}</urlset>'
    ).encode()


@dataclass
class FakeFetcher:
    pages: dict[str, Fetched]
    asked: list[str] = field(default_factory=list)

    async def get(self, url: str, headers: dict[str, str] | None = None) -> Fetched:
        self.asked.append(url)
        return self.pages.get(url) or Fetched(url=url, status=404, body=b"", content_type="")


def _xml(url: str, body: bytes) -> Fetched:
    return Fetched(url=url, status=200, body=body, content_type="application/xml")


async def test_a_rate_limited_sitemap_page_marks_the_walk_incomplete() -> None:
    """Otherwise a sitemap watcher reports every unread URL as gone."""
    root = "https://blog.example.com/sitemap.xml"
    fetcher = FakeFetcher(
        {
            root: _xml(root, _urlset("https://blog.example.com/a.html")),
            f"{root}?page=2": Fetched(
                url=f"{root}?page=2", status=429, body=b"", content_type="text/html"
            ),
        }
    )
    found = await sources.collect_sitemaps(fetcher, [root])  # type: ignore[arg-type]
    assert found.urls == ["https://blog.example.com/a.html"]
    assert found.errors and "429" in found.errors[0]


async def test_a_404_past_the_last_page_is_the_end_not_an_error() -> None:
    root = "https://blog.example.com/sitemap.xml"
    fetcher = FakeFetcher({root: _xml(root, _urlset("https://blog.example.com/a.html"))})
    found = await sources.collect_sitemaps(fetcher, [root])  # type: ignore[arg-type]
    assert found.errors == []


# ── #2: a scope that drifted from its preset is noticed ──────────────────


def test_preset_drift_names_missing_and_retired_rejects() -> None:
    preset = PRESETS["blogger-lean"]
    recommended = [pattern for pattern, _ in preset.reject_patterns]
    scope = Scope(reject_patterns=[*recommended[1:], r"[?&]m=1"])

    missing, retired = discovery_service.preset_drift(scope, preset)
    assert missing == [recommended[0]]
    assert retired == [r"[?&]m=1"]


def test_applying_the_preset_clears_the_drift() -> None:
    preset = PRESETS["blogger-lean"]
    scope = Scope(reject_patterns=[r"[?&]m=1"])
    discovery_service.apply_preset_to_scope(scope, preset, [])
    assert discovery_service.preset_drift(scope, preset) == ([], [])


# ── #22: a site-filtered search is filtered before the candidate cap ─────


def _archived_site(db: Session, settings: Settings, slug: str, pages: dict[str, bytes]) -> None:
    if db.get(Folder, 1) is None:
        db.add(Folder(id=1, name="Unfiled", slug="unfiled", path="Unfiled"))
    site = Site(
        folder_id=1,
        slug=slug,
        title=slug.title(),
        seed_url=f"http://{slug}.test/",
        primary_host=f"{slug}.test",
        archive_path=f"Unfiled/{slug}",
    )
    db.add(site)
    db.flush()
    capture = Capture(
        site_id=site.id,
        kind="full",
        engine_id="wget-warc",
        dir_name="20260811T090000Z-full-wget",
        status="ok",
        started_at=utcnow(),
    )
    db.add(capture)
    db.flush()
    storage.ensure_site_dirs(settings, site.archive_path)
    capture_dir = storage.ensure_capture_dirs(settings, site.archive_path, capture.dir_name)
    write_warc(capture_dir / storage.WARC_DIR / "part-00000.warc.gz", pages)
    textextract.extract_capture(settings, site.archive_path, capture.dir_name)
    search.index_capture(db, settings, site=site, capture=capture)


def test_a_small_site_is_searchable_when_bigger_ones_fill_the_cap(
    db: Session, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _archived_site(
        db,
        settings,
        "big",
        {
            f"http://big.test/{n}.html": blogger_page(
                f"p{n}", "Lighthouse lighthouse", "lighthouse keepers and the lighthouse lamp"
            )
            for n in range(3)
        },
    )
    _archived_site(
        db,
        settings,
        "small",
        {"http://small.test/one.html": blogger_page("one", "Harbour", "a lighthouse far off")},
    )
    monkeypatch.setattr(search, "CANDIDATE_LIMIT", 2)

    small = db.query(Site).filter(Site.slug == "small").one()
    results = search.search(db, settings, query="lighthouse", site_id=small.id)
    assert results.total == 1
    assert results.hits[0].url == "http://small.test/one.html"


# ── live data: Blogger pager variants, not the trail, are the cost ───────


BLOG = "https://example.blogspot.com"


@pytest.mark.parametrize("preset_id", ["blogger", "blogger-lean"])
@pytest.mark.parametrize(
    "url",
    [
        f"{BLOG}/search?updated-max=2026-03-22T00:00:00-04:00&max-results=20&start=11&by-date=false",
        f"{BLOG}/search?updated-max=2026-03-22T00:00:00-04:00&max-results=20&reverse-paginate=true",
        f"{BLOG}/search?updated-min=2026-03-22T00:00:00-04:00&max-results=20&reverse-paginate=true&start=20&by-date=false",
        f"{BLOG}/search?by-date=true",
        f"{BLOG}/search/label/Bra?max-results=20&start=20&by-date=false",
    ],
)
def test_blogger_pager_variants_are_rejected(preset_id: str, url: str) -> None:
    rejects = discovery_service.compiled_rejects(_preset_scope(preset_id))
    assert any(p.search(url) for p in rejects), url


@pytest.mark.parametrize(
    "url",
    [
        # The standard preset's Older-posts trail must survive, or that link
        # is dead in replay (the reason the plain pager was never rejected).
        f"{BLOG}/search?updated-max=2026-06-09T08:00:00-04:00&max-results=20",
        f"{BLOG}/search/label/Bra",
        f"{BLOG}/2026/03/a-post.html",
        f"{BLOG}/2026/03/start-of-something.html",
    ],
)
def test_the_older_posts_trail_and_posts_survive(url: str) -> None:
    rejects = discovery_service.compiled_rejects(_preset_scope("blogger"))
    assert not any(p.search(url) for p in rejects), url


def _preset_scope(preset_id: str) -> Scope:
    scope = Scope()
    discovery_service.apply_preset_to_scope(scope, PRESETS[preset_id], [])
    return scope


# ── live data: Blogger content warnings surfaced as "exited 9" ───────────


def test_a_content_warning_redirect_is_read_out_of_the_crawl_log(tmp_path) -> None:
    from cairn.engines.browsertrix import content_warning_redirects

    post = "https://example.blogspot.com/2026/10/a-post.html"
    log = tmp_path / "crawl.log"
    log.write_text(
        "(node:1) ExperimentalWarning: not JSON\n"
        + json.dumps(
            {
                "logLevel": "warn",
                "context": "recorder",
                "message": "Skipping page that redirects to excluded URL",
                "details": {
                    "newUrl": f"https://www.blogger.com/interstitial/post?u={post}",
                    "origUrl": post,
                },
            }
        )
        + "\n"
        + json.dumps(
            {"logLevel": "error", "message": "Crawl failed, no pages crawled successfully"}
        )
        + "\n",
        encoding="utf-8",
    )
    assert content_warning_redirects(log) == [post]


def test_an_ordinary_failure_is_not_called_a_content_warning(tmp_path) -> None:
    from cairn.engines.browsertrix import content_warning_redirects

    log = tmp_path / "crawl.log"
    log.write_text(
        json.dumps(
            {
                "message": "Skipping page that redirects to excluded URL",
                "details": {"newUrl": "https://elsewhere.example/", "origUrl": "https://a/"},
            }
        ),
        encoding="utf-8",
    )
    assert content_warning_redirects(log) == []
    assert content_warning_redirects(tmp_path / "missing.log") == []


def _feed_with_profile(db: Session, settings: Settings) -> tuple[Feed, AccessProfile]:
    site = site_service.create_site(db, settings, seed_url="https://gated.example.com/")
    profile = AccessProfile(name="gate", mode="interactive")
    db.add(profile)
    db.flush()
    site.profile_id = profile.id
    feed = Feed(site_id=site.id, url="https://gated.example.com/feeds/posts/default")
    db.add(feed)
    db.flush()
    return feed, profile


def test_a_content_warning_block_waits_a_day_not_ten_minutes(
    db: Session, settings: Settings
) -> None:
    from cairn.services.jobs import _settle_feed_items

    feed, _profile = _feed_with_profile(db, settings)
    capture = Capture(site_id=feed.site_id, kind="feed", engine_id="browsertrix", dir_name="x")
    before = utcnow()
    _settle_feed_items(db, [], capture, "failed", feed.id, blocked="content_warning")
    assert feed.next_capture_at is not None
    assert feed.next_capture_at - before > timedelta(hours=20)


def test_saving_a_new_profile_releases_the_held_capture(db: Session, settings: Settings) -> None:
    from cairn.services import profiles

    feed, profile = _feed_with_profile(db, settings)
    feed.capture_failures = 9
    feed.next_capture_at = utcnow() + timedelta(hours=24)
    db.flush()

    assert profiles.release_held_captures(db, profile) == 1
    assert feed.next_capture_at is None
    assert feed.capture_failures == 0
