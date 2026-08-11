"""Text extraction and full-text search.

The measure that matters throughout: a blog's sidebar lists every post title
on every page. If that is indexed, searching a post title matches the whole
blog and the result list is not merely badly ranked but wrong.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from cairn.config import Settings
from cairn.db.models import Capture, Folder, Site
from cairn.db.types import utcnow
from cairn.services import search, storage, textextract

# ── fixture pages ────────────────────────────────────────────────────────

SIDEBAR_TITLES = [f"A post about lens {i}" for i in range(30)]
ARTICLES = {
    "harris": "The wind on Harris was steady enough to blur a six-second exposure into fog. "
    "I had gone looking for the machair in flower and found a tide that would not settle.",
    # Mentions filters in the body only. The post whose *title* is Filters has
    # to outrank it, or title weighting is not doing anything.
    "luskentyre": "Rain at Luskentyre for three days. The sand goes the colour of weak tea "
    "when it is wet, which no photograph I have taken manages to show. Filters made no "
    "difference at all.",
    "tarbert": "A ferry cancelled, so an afternoon in Tarbert instead. The harbour wall is "
    "the only shelter and everyone knows it.",
    "filters": "Notes on filters. A ten-stop is a blunt instrument and I keep reaching for "
    "it anyway, mostly out of habit.",
}


def blogger_page(slug: str, title: str, body: str) -> bytes:
    """Blogger's own markup: single quotes, no <nav>, no <aside>."""
    archive = "".join(
        f"<li><a href='/p/{i}.html'>{t}</a></li>" for i, t in enumerate(SIDEBAR_TITLES)
    )
    return f"""<!DOCTYPE html><html><head><meta content='blogger' name='generator'/>
<title>Coast &amp; Light: {title}</title>
<script type='text/javascript'>var _WidgetManager = {{}};</script></head><body>
<div class='navbar section' id='navbar'><a href='/'>Home</a>
<a href='/p/about.html'>About this blog</a></div>
<div class='main section' id='main'><div class='widget Blog' id='Blog1'>
<h2 class='date-header'><span>Sunday, 4 August 2019</span></h2>
<div class='post hentry'><h3 class='post-title entry-title'>{title}</h3>
<div class='post-body entry-content'><p>{body}</p></div>
<div class='post-footer'>Posted by Ali</div></div></div></div>
<div class='sidebar section' id='sidebar-right-1'>
<div class='widget BlogArchive'><h2>Blog Archive</h2><ul>{archive}</ul></div></div>
<div class='footer section' id='footer'>Powered by Blogger.</div>
</body></html>""".encode()


def plain_page(slug: str, title: str, body: str) -> bytes:
    """A template whose class names say nothing: `left`, `right`, `top`."""
    archive = "".join(f'<li><a href="/p/{i}">{t}</a></li>' for i, t in enumerate(SIDEBAR_TITLES))
    return f"""<html><head><title>{title}</title></head><body>
<div class="wrap"><div class="top"><a href="/">Home</a> <a href="/a">About this blog</a></div>
<div class="left"><h1>{title}</h1><p>{body}</p></div>
<div class="right"><h3>Archive</h3><ul>{archive}</ul></div>
<div class="bottom">Older Post &middot; Newer Post</div></div></body></html>""".encode()


def write_warc(path: Path, pages: dict[str, bytes]) -> None:
    from warcio.statusandheaders import StatusAndHeaders
    from warcio.warcwriter import WARCWriter

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as fh:
        writer = WARCWriter(fh, gzip=True)
        for url, body in pages.items():
            headers = StatusAndHeaders(
                "200 OK",
                [("Content-Type", "text/html; charset=utf-8"), ("Content-Length", str(len(body)))],
                protocol="HTTP/1.1",
            )
            writer.write_record(
                writer.create_warc_record(
                    url, "response", payload=io.BytesIO(body), http_headers=headers
                )
            )


@pytest.fixture
def archived(db: Session, settings: Settings):
    """A site with one capture whose WARC holds four Blogger-shaped posts."""

    def build(builder=blogger_page, host: str = "http://blog.test") -> tuple[Site, Capture]:
        folder = db.get(Folder, 1) or Folder(id=1, name="Unfiled", slug="unfiled", path="Unfiled")
        if folder.id is None:  # pragma: no cover
            db.add(folder)
        site = Site(
            folder_id=1,
            slug="coast",
            title="Coast & Light",
            seed_url=f"{host}/",
            primary_host="blog.test",
            archive_path="Unfiled/coast",
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
        write_warc(
            capture_dir / storage.WARC_DIR / "part-00000.warc.gz",
            {
                f"{host}/{slug}.html": builder(slug, slug.title(), body)
                for slug, body in ARTICLES.items()
            },
        )
        return site, capture

    return build


# ── extraction ───────────────────────────────────────────────────────────


def test_class_names_keep_the_sidebar_out_of_one_page() -> None:
    """The class rules on their own, with no corpus to fall back on.

    One page rather than a capture, deliberately. Given several pages the
    repetition filter removes the sidebar whether or not the class rules ever
    recognised it — so the same assertions over a whole capture pass with
    these rules deleted, and prove nothing about them.
    """
    title, blocks = textextract.parse(blogger_page("harris", "Harris", ARTICLES["harris"]).decode())
    text = "\n".join(blocks)

    assert "Coast & Light" in title
    assert "machair in flower" in text
    assert not any(t in text for t in SIDEBAR_TITLES)
    assert "Powered by Blogger" not in text
    assert "_WidgetManager" not in text
    assert "About this blog" not in text


def test_a_whole_capture_comes_out_with_the_furniture_gone(
    archived, db: Session, settings: Settings
) -> None:
    site, capture = archived()
    result = textextract.extract_capture(settings, site.archive_path, capture.dir_name)

    assert len(result.pages) == 4
    joined = "\n".join(p.text for p in result.pages)
    assert "machair in flower" in joined
    assert not any(t in joined for t in SIDEBAR_TITLES)
    assert "Powered by Blogger" not in joined
    assert "_WidgetManager" not in joined


def test_the_post_title_survives_extraction(archived, db: Session, settings: Settings) -> None:
    """WordPress wraps it in `entry-header`, which contains the word `header`.
    Matching that as boilerplate would drop the title of every post."""
    title, blocks = textextract.parse(
        '<html><head><title>Doc</title></head><body><header class="site-header">Coast</header>'
        '<article><header class="entry-header"><h1 class="entry-title">Long exposures</h1>'
        "</header><div class='entry-content'><p>The machair in flower on Harris.</p></div>"
        "</article></body></html>"
    )
    text = "\n".join(blocks)
    assert title == "Doc"
    assert "Long exposures" in text
    assert "machair in flower" in text


def test_repetition_catches_what_class_names_cannot(
    archived, db: Session, settings: Settings
) -> None:
    """A template that names its columns `left` and `right` gives the class
    rules nothing to work with. The blocks repeated across the capture do."""
    site, capture = archived(builder=plain_page)
    result = textextract.extract_capture(settings, site.archive_path, capture.dir_name)

    joined = "\n".join(p.text for p in result.pages)
    assert "machair in flower" in joined
    assert not any(t in joined for t in SIDEBAR_TITLES)
    assert "About this blog" not in joined
    assert result.dropped_blocks > 0


def test_a_single_page_capture_keeps_everything(settings: Settings, tmp_path: Path) -> None:
    """With one page there is no such thing as a block on every page, and a
    sidebar shared with pages nobody captured does no harm."""
    root = storage.ensure_site_dirs(settings, "Unfiled/solo")
    capture_dir = storage.ensure_capture_dirs(
        settings, "Unfiled/solo", "20260811T090000Z-full-wget"
    )
    write_warc(
        capture_dir / storage.WARC_DIR / "part-00000.warc.gz",
        {"http://blog.test/one.html": plain_page("one", "One", ARTICLES["harris"])},
    )
    result = textextract.extract_capture(settings, "Unfiled/solo", "20260811T090000Z-full-wget")

    assert root.exists()
    assert result.dropped_blocks == 0
    assert "machair in flower" in result.pages[0].text


def test_extraction_writes_a_seekable_file(archived, settings: Settings) -> None:
    site, capture = archived()
    result = textextract.extract_capture(settings, site.archive_path, capture.dir_name)
    assert result.path is not None

    for page in result.pages:
        again = textextract.read_page_at(result.path, page.offset, page.length)
        assert again is not None
        assert again.url == page.url
        assert again.text == page.text


def test_decoding_believes_the_header_then_the_document() -> None:
    body = "Café".encode("iso-8859-1")
    assert textextract.decode(body, "text/html; charset=iso-8859-1") == "Café"
    meta = b'<meta charset="iso-8859-1">' + body
    assert "Café" in textextract.decode(meta, "text/html")
    # Nonsense in the header must not raise; the page is still worth indexing.
    assert textextract.decode(body, "text/html; charset=not-a-charset")


# ── query parsing ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw",
    ["c++", 'don"t', "foo:bar", "NEAR", "*", '"', "-", "OR", "AND", "a AND b", "()", "^x", "a-b"],
)
def test_no_search_box_input_is_a_syntax_error(
    raw: str, archived, db: Session, settings: Settings
) -> None:
    """Every one of these is either an operator or invalid inside MATCH. A
    search box that raises `fts5: syntax error near` is one people stop using."""
    site, capture = archived()
    textextract.extract_capture(settings, site.archive_path, capture.dir_name)
    search.index_capture(db, settings, site=site, capture=capture)

    search.search(db, settings, query=raw)  # must not raise


def test_operators_are_words_not_operators(archived, db: Session, settings: Settings) -> None:
    site, capture = archived()
    textextract.extract_capture(settings, site.archive_path, capture.dir_name)
    search.index_capture(db, settings, site=site, capture=capture)

    # "harris AND ferry" appears on no page; treating AND as an operator would
    # return the Tarbert post, which mentions a ferry.
    assert search.search(db, settings, query="harris AND ferry").total == 0
    assert search.search(db, settings, query="harris").total == 1


def test_a_phrase_is_not_the_same_as_its_words(archived, db: Session, settings: Settings) -> None:
    site, capture = archived()
    textextract.extract_capture(settings, site.archive_path, capture.dir_name)
    search.index_capture(db, settings, site=site, capture=capture)

    assert search.search(db, settings, query='"machair in flower"').total == 1
    assert search.search(db, settings, query='"flower in machair"').total == 0


def test_prefix_and_exclusion(archived, db: Session, settings: Settings) -> None:
    site, capture = archived()
    textextract.extract_capture(settings, site.archive_path, capture.dir_name)
    search.index_capture(db, settings, site=site, capture=capture)

    assert search.search(db, settings, query="expos*").total == 1
    both = search.search(db, settings, query="the").total
    assert both > 1
    assert search.search(db, settings, query="the -harris").total < both


def test_a_query_of_only_exclusions_returns_nothing(
    archived, db: Session, settings: Settings
) -> None:
    """`NOT` cannot open an FTS5 expression, and "everything except x" is not
    a question a search box should answer with the whole archive."""
    site, capture = archived()
    textextract.extract_capture(settings, site.archive_path, capture.dir_name)
    search.index_capture(db, settings, site=site, capture=capture)

    assert search.search(db, settings, query="-harris").total == 0


# ── searching ────────────────────────────────────────────────────────────


def test_a_post_title_matches_one_post_not_the_whole_blog(
    archived, db: Session, settings: Settings
) -> None:
    """The feature, stated as a test. Every page carries a sidebar listing
    every post title; indexed naively, one title matches all of them."""
    site, capture = archived()
    textextract.extract_capture(settings, site.archive_path, capture.dir_name)
    indexed = search.index_capture(db, settings, site=site, capture=capture)
    assert indexed == 4

    results = search.search(db, settings, query='"A post about lens 7"')
    assert results.total == 0

    results = search.search(db, settings, query='"machair in flower"')
    assert results.total == 1
    assert results.hits[0].url.endswith("/harris.html")


def test_results_carry_a_snippet_around_the_term(archived, db: Session, settings: Settings) -> None:
    site, capture = archived()
    textextract.extract_capture(settings, site.archive_path, capture.dir_name)
    search.index_capture(db, settings, site=site, capture=capture)

    hit = search.search(db, settings, query="machair").hits[0]
    assert hit.snippets
    assert "machair" in hit.snippets[0].lower()
    assert hit.timestamp and len(hit.timestamp) == 14
    assert hit.capture_id == capture.id


def test_a_title_match_outranks_a_body_match(archived, db: Session, settings: Settings) -> None:
    site, capture = archived()
    textextract.extract_capture(settings, site.archive_path, capture.dir_name)
    search.index_capture(db, settings, site=site, capture=capture)

    results = search.search(db, settings, query="filters")
    assert results.total >= 2
    assert results.hits[0].url.endswith("/filters.html")


def test_recapturing_a_page_replaces_it_rather_than_duplicating_it(
    archived, db: Session, settings: Settings
) -> None:
    """One row per (site, url). Otherwise a site captured weekly returns the
    same page fifty-two times and the result list is a changelog."""
    site, capture = archived()
    textextract.extract_capture(settings, site.archive_path, capture.dir_name)
    search.index_capture(db, settings, site=site, capture=capture)

    second = Capture(
        site_id=site.id,
        kind="full",
        engine_id="wget-warc",
        dir_name="20260812T090000Z-full-wget",
        status="ok",
        started_at=utcnow(),
    )
    db.add(second)
    db.flush()
    capture_dir = storage.ensure_capture_dirs(settings, site.archive_path, second.dir_name)
    write_warc(
        capture_dir / storage.WARC_DIR / "part-00000.warc.gz",
        {
            f"http://blog.test/{slug}.html": blogger_page(slug, slug.title(), body)
            for slug, body in ARTICLES.items()
        },
    )
    textextract.extract_capture(settings, site.archive_path, second.dir_name)
    search.index_capture(db, settings, site=site, capture=second)

    results = search.search(db, settings, query='"machair in flower"')
    assert results.total == 1
    assert results.hits[0].capture_id == second.id


def test_deleting_a_capture_takes_its_pages_out_of_the_index(
    archived, db: Session, settings: Settings
) -> None:
    site, capture = archived()
    textextract.extract_capture(settings, site.archive_path, capture.dir_name)
    search.index_capture(db, settings, site=site, capture=capture)
    assert search.search(db, settings, query="machair").total == 1

    assert search.drop_capture(db, capture.id) == 4
    assert search.search(db, settings, query="machair").total == 0


def test_reindexing_reads_the_text_files_not_the_warcs(
    archived, db: Session, settings: Settings
) -> None:
    """The index is derived data. Rebuilding it must not need the archive,
    which on a NAS is cold and large."""
    site, capture = archived()
    textextract.extract_capture(settings, site.archive_path, capture.dir_name)
    search.index_capture(db, settings, site=site, capture=capture)

    warc = (
        storage.site_dir(settings, site.archive_path)
        / storage.CAPTURES_DIR
        / capture.dir_name
        / storage.WARC_DIR
        / "part-00000.warc.gz"
    )
    warc.unlink()

    assert search.reindex_site(db, settings, site) == 4
    assert search.search(db, settings, query='"machair in flower"').total == 1


def test_search_can_be_scoped_to_one_site(archived, db: Session, settings: Settings) -> None:
    site, capture = archived()
    textextract.extract_capture(settings, site.archive_path, capture.dir_name)
    search.index_capture(db, settings, site=site, capture=capture)

    assert search.search(db, settings, query="machair", site_id=site.id).total == 1
    assert search.search(db, settings, query="machair", site_id=site.id + 999).total == 0


def test_a_deleted_site_disappears_from_results(archived, db: Session, settings: Settings) -> None:
    """Trashed, not purged: the rows are still there and must not be found."""
    site, capture = archived()
    textextract.extract_capture(settings, site.archive_path, capture.dir_name)
    search.index_capture(db, settings, site=site, capture=capture)

    site.deleted_at = utcnow()
    db.flush()
    assert search.search(db, settings, query="machair").total == 0


def test_the_index_holds_no_copy_of_the_text(archived, db: Session, settings: Settings) -> None:
    """Contentless FTS5, asserted rather than assumed: the terms are indexed
    and the document is not stored, which is what keeps a database that gets
    backed up before every migration from carrying the whole archive."""
    from sqlalchemy import text as sql

    site, capture = archived()
    textextract.extract_capture(settings, site.archive_path, capture.dir_name)
    search.index_capture(db, settings, site=site, capture=capture)

    # snippet() is NULL on a contentless table — the reason snippets are built
    # from the file on disk.
    row = db.execute(
        sql(
            "SELECT snippet(page_text_fts, 1, '[', ']', '…', 8) FROM page_text_fts "
            "WHERE page_text_fts MATCH '\"machair\"'"
        )
    ).first()
    assert row is not None and row[0] is None
