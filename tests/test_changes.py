"""Capture diffing, page watching, and what retention refuses to delete.

The retention half is the one to read carefully. Every test here is about a
protection holding, because a retention feature that deletes the wrong capture
is worse than having none — the archive's whole value is the content that is
no longer anywhere else.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy.orm import Session

from cairn.config import Settings
from cairn.db.models import Capture, Feed, FeedItem, Site
from cairn.db.types import utcnow
from cairn.services import diffs, feeds, replay, retention, storage, textextract
from tests.test_search import blogger_page, write_warc

ARTICLE = "The wind on Harris was steady enough to blur a six-second exposure into fog."
EDITED = "The wind on Harris was steady enough to blur an eight-second exposure into fog."


def make_site(db: Session, settings: Settings, *, slug: str = "coast") -> Site:
    site = Site(
        folder_id=1,
        slug=slug,
        title="Coast & Light",
        seed_url="http://blog.test/",
        primary_host="blog.test",
        archive_path=f"Unfiled/{slug}",
    )
    db.add(site)
    db.flush()
    storage.ensure_site_dirs(settings, site.archive_path)
    return site


def add_capture(
    db: Session,
    settings: Settings,
    site: Site,
    *,
    dir_name: str,
    pages: dict[str, bytes],
    started_at: object = None,
    extract: bool = True,
) -> Capture:
    capture = Capture(
        site_id=site.id,
        kind="full",
        engine_id="wget-warc",
        dir_name=dir_name,
        status="ok",
        started_at=started_at or utcnow(),
    )
    db.add(capture)
    db.flush()
    capture_dir = storage.ensure_capture_dirs(settings, site.archive_path, dir_name)
    write_warc(capture_dir / storage.WARC_DIR / "part-00000.warc.gz", pages)
    if extract:
        textextract.extract_capture(settings, site.archive_path, dir_name)
    return capture


def post(slug: str, body: str) -> bytes:
    return blogger_page(slug, slug.title(), body)


# ── diffing ──────────────────────────────────────────────────────────────


def test_an_edited_sentence_shows_as_a_word_change() -> None:
    changes, ratio = diffs.diff_blocks([ARTICLE], [EDITED])
    assert len(changes) == 1
    assert changes[0].kind == "changed"
    assert [(w.before, w.after) for w in changes[0].words] == [("a six-second", "an eight-second")]
    assert ratio == 1.0


def test_added_and_removed_blocks_are_named_as_such() -> None:
    changes, _ratio = diffs.diff_blocks(["one", "two", "three"], ["one", "three", "four"])
    kinds = [(c.kind, c.before or c.after) for c in changes]
    assert ("removed", "two") in kinds
    assert ("added", "four") in kinds


def test_an_unchanged_page_diffs_to_nothing() -> None:
    changes, ratio = diffs.diff_blocks([ARTICLE, "Kit: a filter."], [ARTICLE, "Kit: a filter."])
    assert changes == []
    assert ratio == 0.0


def test_comparing_two_captures_names_the_page_that_changed(
    db: Session, settings: Settings
) -> None:
    site = make_site(db, settings)
    before = add_capture(
        db,
        settings,
        site,
        dir_name="20260801T090000Z-full-wget",
        pages={
            "http://blog.test/harris.html": post("harris", ARTICLE),
            "http://blog.test/tarbert.html": post("tarbert", "A ferry cancelled."),
        },
    )
    after = add_capture(
        db,
        settings,
        site,
        dir_name="20260901T090000Z-full-wget",
        pages={
            "http://blog.test/harris.html": post("harris", EDITED),
            "http://blog.test/tarbert.html": post("tarbert", "A ferry cancelled."),
            "http://blog.test/new.html": post("new", "A corncrake heard and never seen."),
        },
    )

    result = diffs.compare_captures(settings, site, before=before, after=after)
    assert result.changed == 1
    assert result.added == 1
    assert result.removed == 0
    assert result.unchanged == 1
    assert result.pages[0].url.endswith("/harris.html")
    assert result.pages[0].kind == "changed"


def test_one_page_across_two_captures(db: Session, settings: Settings) -> None:
    site = make_site(db, settings)
    before = add_capture(
        db,
        settings,
        site,
        dir_name="20260801T090000Z-full-wget",
        pages={"http://blog.test/harris.html": post("harris", ARTICLE)},
    )
    after = add_capture(
        db,
        settings,
        site,
        dir_name="20260901T090000Z-full-wget",
        pages={"http://blog.test/harris.html": post("harris", EDITED)},
    )

    diff = diffs.compare_page(
        settings, site, before=before, after=after, url="http://blog.test/harris.html"
    )
    assert diff.changed
    words = [w for block in diff.blocks for w in block.words]
    assert any("eight-second" in w.after for w in words)


def test_a_capture_with_no_extracted_text_says_so(db: Session, settings: Settings) -> None:
    """Silence would read as "nothing changed", which is a different claim."""
    site = make_site(db, settings)
    before = add_capture(
        db,
        settings,
        site,
        dir_name="20260801T090000Z-full-wget",
        pages={"http://blog.test/harris.html": post("harris", ARTICLE)},
        extract=False,
    )
    after = add_capture(
        db,
        settings,
        site,
        dir_name="20260901T090000Z-full-wget",
        pages={"http://blog.test/harris.html": post("harris", EDITED)},
        extract=False,
    )
    result = diffs.compare_captures(settings, site, before=before, after=after)
    assert "Rebuild search index" in result.note


def test_a_replaced_asset_shows_up_under_the_same_url(db: Session, settings: Settings) -> None:
    """The change that no page's text mentions: same URL, different bytes."""
    site = make_site(db, settings)
    before = add_capture(
        db,
        settings,
        site,
        dir_name="20260801T090000Z-full-wget",
        pages={
            "http://blog.test/harris.html": post("harris", ARTICLE),
            "http://blog.test/logo.png": b"\x89PNG\r\n\x1a\nORIGINAL",
        },
    )
    after = add_capture(
        db,
        settings,
        site,
        dir_name="20260901T090000Z-full-wget",
        pages={
            "http://blog.test/harris.html": post("harris", ARTICLE),
            "http://blog.test/logo.png": b"\x89PNG\r\n\x1a\nREPLACED-ENTIRELY",
        },
    )
    replay.build_index(settings, site.archive_path)

    changes = diffs.compare_resources(settings, site, before=before, after=after)
    logo = [c for c in changes if c.url.endswith("/logo.png")]
    assert len(logo) == 1
    assert logo[0].kind == "changed"
    assert logo[0].before_digest and logo[0].before_digest != logo[0].after_digest


# ── the page watcher ─────────────────────────────────────────────────────


def watcher(db: Session, site: Site, url: str = "http://blog.test/about.html") -> Feed:
    return feeds.add_feed(db, site, url=url, kind="page")


def poll_with(
    db: Session, feed: Feed, *, entries: list[feeds.Entry], first: bool = False
) -> object:
    result = feeds.FetchResult(status=200, parsed=feeds.ParsedFeed(kind="page", entries=entries))
    return feeds.apply(db, feed, result)


def entry_for(url: str, digest: str, title: str = "About") -> feeds.Entry:
    canonical = feeds.canonical_url(url)
    return feeds.Entry(
        guid=canonical, url=url, canonical=canonical, title=title, content_hash=digest
    )


def test_a_watched_page_is_baselined_before_it_is_watched(db: Session, settings: Settings) -> None:
    """Adding a watcher to a page must not immediately capture it. The site is
    already archived; the watcher is for what happens next."""
    site = make_site(db, settings)
    feed = watcher(db, site)
    outcome = poll_with(db, feed, entries=[entry_for("http://blog.test/about.html", "aaa")])

    assert outcome.baseline is True
    assert outcome.new_items == []
    item = db.scalars(feeds.select(FeedItem).where(FeedItem.feed_id == feed.id)).one()
    assert item.content_hash == "aaa"
    assert item.status == "skipped"


def test_an_unchanged_page_produces_nothing(db: Session, settings: Settings) -> None:
    site = make_site(db, settings)
    feed = watcher(db, site)
    poll_with(db, feed, entries=[entry_for("http://blog.test/about.html", "aaa")])
    outcome = poll_with(db, feed, entries=[entry_for("http://blog.test/about.html", "aaa")])

    assert outcome.new_items == []
    assert outcome.action == "no change"


def test_changed_text_makes_the_page_pending(db: Session, settings: Settings) -> None:
    site = make_site(db, settings)
    feed = watcher(db, site)
    poll_with(db, feed, entries=[entry_for("http://blog.test/about.html", "aaa")])
    outcome = poll_with(db, feed, entries=[entry_for("http://blog.test/about.html", "bbb")])

    assert len(outcome.new_items) == 1
    item = db.get(FeedItem, outcome.new_items[0])
    assert item.status == "pending"
    assert item.content_hash == "bbb"


def test_the_watcher_hashes_the_text_not_the_markup() -> None:
    """Three fetches of one unchanged post, with a visit counter, a rotating
    ad slot, a comment count and a timestamp in the furniture. Hashing the
    body would report a change every poll, forever."""
    import hashlib

    def render(n: int, body: str) -> str:
        return f"""<html><head><title>About</title>
<script>var ad = "slot-{n * 977}";</script></head><body>
<div class='navbar section'><span>Visits: {10000 + n}</span></div>
<div class='main section'><div class='post hentry'>
<div class='post-body entry-content'><p>{body}</p></div>
<div class='post-footer'>{n} comments</div></div></div>
<div class='footer section'>Generated at 2026-08-11T20:{n:02d}:00Z.</div>
</body></html>"""

    def text_hash(html: str) -> str:
        _title, blocks = textextract.parse(html)
        return hashlib.sha256("\n".join(blocks).encode()).hexdigest()

    unchanged = {text_hash(render(n, ARTICLE)) for n in (1, 2, 3)}
    raw = {hashlib.sha256(render(n, ARTICLE).encode()).hexdigest() for n in (1, 2, 3)}

    assert len(unchanged) == 1, "the extracted text moved when only the furniture did"
    assert len(raw) == 3, "the fixture is not actually rotating anything"
    assert text_hash(render(4, EDITED)) not in unchanged


def test_a_watched_page_never_reports_anything_as_gone(db: Session, settings: Settings) -> None:
    """Only a complete sitemap may infer disappearance. A page that failed to
    load is not a page that was deleted."""
    site = make_site(db, settings)
    feed = watcher(db, site)
    poll_with(db, feed, entries=[entry_for("http://blog.test/about.html", "aaa")])
    outcome = poll_with(db, feed, entries=[entry_for("http://blog.test/about.html", "bbb")])
    assert outcome.gone_items == []


def test_a_page_watcher_polls_less_often_than_a_feed() -> None:
    assert feeds.default_interval("page") > feeds.default_interval("rss")
    assert feeds.default_interval("page") < feeds.default_interval("sitemap")


# ── retention ────────────────────────────────────────────────────────────


def series(
    db: Session, settings: Settings, site: Site, count: int, **kwargs: object
) -> list[Capture]:
    """`count` monthly captures of the same two pages, oldest first."""
    base = utcnow() - timedelta(days=40 * count)
    out = []
    for n in range(count):
        out.append(
            add_capture(
                db,
                settings,
                site,
                dir_name=f"2026{n + 1:02d}01T090000Z-full-wget",
                pages={
                    "http://blog.test/harris.html": post("harris", f"{ARTICLE} Revision {n}."),
                    "http://blog.test/tarbert.html": post("tarbert", "A ferry cancelled."),
                },
                started_at=base + timedelta(days=30 * n),
                **kwargs,  # type: ignore[arg-type]
            )
        )
    return out


def set_policy(db: Session, site: Site, **policy: object) -> None:
    scope_settings = dict(site.scope_settings or {})
    scope_settings["retention"] = {"enabled": True, **policy}
    site.scope_settings = scope_settings
    db.flush()


def record_urls(db: Session, capture: Capture, urls: list[str]) -> None:
    from cairn.db.models import CaptureUrl

    for url in urls:
        db.add(CaptureUrl(capture_id=capture.id, url=url, host="blog.test", status_code=200))
    db.flush()


def test_nothing_is_prunable_by_default(db: Session, settings: Settings) -> None:
    """Retention is off until somebody turns it on, and the dry run still
    works — that is how anybody decides whether to turn it on."""
    site = make_site(db, settings)
    captures = series(db, settings, site, 6)
    for capture in captures:
        record_urls(db, capture, ["http://blog.test/harris.html", "http://blog.test/tarbert.html"])

    plan = retention.plan(db, settings, site)
    assert plan.policy["enabled"] is False
    assert len(plan.decisions) == 6
    # The plan is computed regardless; nothing acts on it.
    assert retention.due_sites(db, settings) == []


def test_the_first_capture_is_never_pruned(db: Session, settings: Settings) -> None:
    site = make_site(db, settings)
    captures = series(db, settings, site, 6)
    for capture in captures:
        record_urls(db, capture, ["http://blog.test/harris.html", "http://blog.test/tarbert.html"])
    set_policy(db, site, keep_last=1, keep_monthly=0, min_age_days=0)

    plan = retention.plan(db, settings, site)
    first = next(d for d in plan.decisions if d.capture_id == captures[0].id)
    assert first.keep
    assert first.reason == "first"


def test_the_newest_captures_are_kept(db: Session, settings: Settings) -> None:
    site = make_site(db, settings)
    captures = series(db, settings, site, 6)
    for capture in captures:
        record_urls(db, capture, ["http://blog.test/harris.html", "http://blog.test/tarbert.html"])
    set_policy(db, site, keep_last=2, keep_monthly=0, min_age_days=0)

    plan = retention.plan(db, settings, site)
    kept = {d.capture_id for d in plan.decisions if d.keep}
    assert captures[-1].id in kept
    assert captures[-2].id in kept
    assert plan.prunable, "with six captures and keep_last=2 something must be prunable"


def test_a_recent_capture_is_too_young_to_prune(db: Session, settings: Settings) -> None:
    site = make_site(db, settings)
    captures = series(db, settings, site, 4)
    for capture in captures:
        record_urls(db, capture, ["http://blog.test/harris.html"])
    set_policy(db, site, keep_last=0, keep_monthly=0, min_age_days=10_000)

    plan = retention.plan(db, settings, site)
    assert not plan.prunable
    assert {d.reason for d in plan.decisions} <= {"first", "min-age", "last-copy"}


def test_the_last_copy_of_a_vanished_page_is_protected(db: Session, settings: Settings) -> None:
    """The clause the whole feature exists for. A post deleted upstream lives
    only in the captures made before it went, and naive retention deletes
    exactly those."""
    site = make_site(db, settings)
    captures = series(db, settings, site, 5)
    everything = ["http://blog.test/harris.html", "http://blog.test/tarbert.html"]
    for capture in captures[:3]:
        record_urls(db, capture, [*everything, "http://blog.test/deleted-post.html"])
    for capture in captures[3:]:
        record_urls(db, capture, everything)
    set_policy(db, site, keep_last=1, keep_monthly=0, min_age_days=0)

    plan = retention.plan(db, settings, site)
    by_id = {d.capture_id: d for d in plan.decisions}

    # Capture 3 is the last one holding the deleted post, so it stays.
    assert by_id[captures[2].id].keep
    assert by_id[captures[2].id].reason == "last-copy"
    assert "deleted-post" in by_id[captures[2].id].detail
    # Captures 1 and 2 also hold it, but they are not the last copy — except
    # for capture 1, which is protected for being the first.
    assert by_id[captures[1].id].keep is False
    assert by_id[captures[0].id].reason == "first"


def test_a_capture_that_a_later_one_deduplicates_against_is_protected(
    db: Session, settings: Settings
) -> None:
    """Measured against a real pywb: prune the capture a revisit points at and
    replay answers 503 for a page whose own capture is entirely intact.

    Four captures, arranged so the dedup source is the *second* — protected by
    nothing else. The first is protected for being first and the fourth for
    being newest, so if this clause were missing, the second would be prunable
    and the fourth would break the moment it went.
    """
    site = make_site(db, settings)
    days = 400
    made = []
    for n, pages in enumerate(
        [
            {"http://blog.test/harris.html": post("harris", ARTICLE)},
            {"http://blog.test/tarbert.html": post("tarbert", "A ferry cancelled.")},
            {"http://blog.test/filters.html": post("filters", "A ten-stop is blunt.")},
            {},
        ]
    ):
        made.append(
            add_capture(
                db,
                settings,
                site,
                dir_name=f"2026{n + 1:02d}01T090000Z-full-wget",
                pages=pages,
                started_at=utcnow() - timedelta(days=days - n * 100),
            )
        )
    first, source, spare, newest = made

    _write_revisit_of(
        settings, site, source=source, into=newest, url="http://blog.test/tarbert.html"
    )
    replay.build_index(settings, site.archive_path)
    everything = ["http://blog.test/harris.html", "http://blog.test/tarbert.html"]
    for capture in made:
        record_urls(db, capture, everything)
    set_policy(db, site, keep_last=1, keep_monthly=0, min_age_days=0)

    plan = retention.plan(db, settings, site)
    by_id = {d.capture_id: d for d in plan.decisions}

    assert by_id[source.id].keep
    assert by_id[source.id].reason == "dedup-source"
    assert newest.dir_name in by_id[source.id].detail
    assert by_id[first.id].reason == "first"
    assert by_id[newest.id].reason == "newest"
    # And the one nothing depends on is still prunable, or the clause is just
    # protecting everything.
    assert by_id[spare.id].keep is False


def _write_revisit_of(
    settings: Settings, site: Site, *, source: Capture, into: Capture, url: str
) -> None:
    """Put a revisit record into `into` pointing at `source`'s response."""
    from warcio.archiveiterator import ArchiveIterator
    from warcio.statusandheaders import StatusAndHeaders
    from warcio.warcwriter import WARCWriter

    root = storage.site_dir(settings, site.archive_path) / storage.CAPTURES_DIR
    original = root / source.dir_name / storage.WARC_DIR / "part-00000.warc.gz"
    digest = date = ""
    with open(original, "rb") as fh:
        for record in ArchiveIterator(fh):
            if record.rec_headers.get_header("WARC-Target-URI") == url:
                digest = record.rec_headers.get_header("WARC-Payload-Digest")
                date = record.rec_headers.get_header("WARC-Date")
                break
    assert digest, "the source capture has no such URL"

    target = root / into.dir_name / storage.WARC_DIR / "part-00000.warc.gz"
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "wb") as fh:
        writer = WARCWriter(fh, gzip=True)
        headers = StatusAndHeaders("200 OK", [("Content-Type", "text/html")], protocol="HTTP/1.1")
        writer.write_record(
            writer.create_revisit_record(
                url, digest=digest, refers_to_uri=url, refers_to_date=date, http_headers=headers
            )
        )


def test_applying_a_plan_deletes_the_files_and_rebuilds_the_index(
    db: Session, settings: Settings
) -> None:
    site = make_site(db, settings)
    captures = series(db, settings, site, 5)
    for capture in captures:
        record_urls(db, capture, ["http://blog.test/harris.html", "http://blog.test/tarbert.html"])
    replay.build_index(settings, site.archive_path)
    set_policy(db, site, keep_last=1, keep_monthly=0, min_age_days=0)

    plan = retention.plan(db, settings, site)
    doomed = [d.dir_name for d in plan.prunable]
    assert doomed

    result = retention.apply_plan(db, settings, site, plan)
    assert sorted(result.pruned) == sorted(doomed)
    assert result.freed_bytes > 0

    root = storage.site_dir(settings, site.archive_path) / storage.CAPTURES_DIR
    for name in doomed:
        assert not (root / name).exists()

    index = replay.index_path(settings, site.archive_path).read_text(encoding="utf-8")
    for name in doomed:
        assert name not in index


def test_applying_a_stale_plan_refuses_what_is_now_protected(
    db: Session, settings: Settings
) -> None:
    """A plan is made in a browser tab and applied a few minutes later. In
    between, a capture can become the last copy of something."""
    site = make_site(db, settings)
    captures = series(db, settings, site, 4)
    for capture in captures:
        record_urls(db, capture, ["http://blog.test/harris.html"])
    set_policy(db, site, keep_last=1, keep_monthly=0, min_age_days=0)

    plan = retention.plan(db, settings, site)
    assert plan.prunable

    # Something the plan did not know about: the second capture turns out to
    # hold the only copy of a page.
    record_urls(db, captures[1], ["http://blog.test/only-here.html"])

    result = retention.apply_plan(db, settings, site, plan)
    assert any("protected now" in problem for problem in result.errors)
    assert captures[1].dir_name not in result.pruned


def test_pruning_takes_the_search_rows_and_the_text_with_it(
    db: Session, settings: Settings
) -> None:
    from cairn.services import search

    site = make_site(db, settings)
    captures = series(db, settings, site, 4)
    for capture in captures:
        record_urls(db, capture, ["http://blog.test/harris.html"])
        search.index_capture(db, settings, site=site, capture=capture)
    set_policy(db, site, keep_last=1, keep_monthly=0, min_age_days=0)

    plan = retention.plan(db, settings, site)
    doomed = plan.prunable[0]
    text_file = textextract.text_path(settings, site.archive_path, doomed.dir_name)
    assert text_file.is_file()

    retention.apply_plan(db, settings, site, plan)
    assert not text_file.exists()
    assert search.search(db, settings, query='"machair"').total <= 1
