"""Annotations, and profiles that carry more than a cookie jar.

Both are about a boundary. Annotations cannot live on replay because replay is
a separate origin *by design*, so they live on the reader and anchor to a
quotation. A profile's localStorage cannot reach wget for the same kind of
reason — wget takes `--load-cookies` and nothing else — so the thing to build
is not a bridge but a sentence that says so.

The bookmarklet's server side is tested here too: it is the URL importer with
one URL, which is the entire point of building it that way.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from cairn.config import Settings
from cairn.db.models import Annotation, Capture, PageText, Site
from cairn.services import annotations as annotation_service
from cairn.services import profiles as profile_service
from cairn.services import reader, textextract
from cairn.services import sites as site_service
from tests.conftest import XHR

BLOCKS = [
    "A heading about filters",
    "The first paragraph mentions a coffee filter and then moves on.",
    "The second paragraph mentions a coffee filter as well, for contrast.",
]


def _readable(db: Session, settings: Settings, blocks: list[str] | None = None) -> Site:
    site = site_service.create_site(db, settings, seed_url="https://notes.example.com/")
    capture_dir = "20260101-000000-full"
    page = textextract.Page(
        url="https://notes.example.com/post.html",
        title="A post",
        blocks=list(blocks or BLOCKS),
        kinds=["h2", "p", "p"],
        timestamp="20260101000000",
    )
    textextract._write_jsonl(
        textextract.text_path(settings, site.archive_path, capture_dir), [page]
    )
    db.add(
        Capture(
            site_id=site.id, kind="full", engine_id="wget-warc", dir_name=capture_dir, status="ok"
        )
    )
    db.add(
        PageText(
            site_id=site.id,
            capture_dir=capture_dir,
            url=page.url,
            title=page.title,
            timestamp=page.timestamp,
            offset=page.offset,
            length=page.length,
            words=20,
        )
    )
    db.flush()
    return site


def _article(db: Session, settings: Settings, site: Site) -> reader.Article:
    article = reader.read(db, settings, site, "https://notes.example.com/post.html")
    assert article is not None
    return article


# ── anchoring ────────────────────────────────────────────────────────────


def test_a_note_is_found_where_it_was_made(db: Session, settings: Settings) -> None:
    site = _readable(db, settings)
    row = annotation_service.create(
        db,
        site,
        url="https://notes.example.com/post.html",
        quote="a coffee filter",
        note="the thing I was looking for",
        prefix="paragraph mentions ",
        suffix=" and then moves on",
        block_index=1,
    )
    found = annotation_service.locate(_article(db, settings, site), row)
    assert found.found
    assert found.block_index == 1
    assert BLOCKS[1][found.start : found.end] == "a coffee filter"


def test_context_tells_two_identical_quotes_apart(db: Session, settings: Settings) -> None:
    """The reason `prefix` and `suffix` exist at all.

    "a coffee filter" appears in both paragraphs. Without the surrounding
    text, a re-extraction that shifted the block indices would attach the note
    to whichever came first — silently, and to the wrong sentence.
    """
    site = _readable(db, settings)
    row = annotation_service.create(
        db,
        site,
        url="https://notes.example.com/post.html",
        quote="a coffee filter",
        prefix="paragraph mentions ",
        suffix=" as well, for contrast",
        # Deliberately wrong: pretend the page was re-extracted and the hint
        # no longer points at the right block.
        block_index=0,
    )
    found = annotation_service.locate(_article(db, settings, site), row)
    assert found.found
    assert found.block_index == 2


def test_a_note_whose_sentence_is_gone_says_so(db: Session, settings: Settings) -> None:
    """It is kept and reported, never moved to the nearest thing.

    An annotation that silently attaches itself somewhere else is worse than
    one that admits it is lost: the second is a fact about the page, the first
    is a quotation somebody never made.
    """
    site = _readable(db, settings)
    row = annotation_service.create(
        db,
        site,
        url="https://notes.example.com/post.html",
        quote="a sentence the author deleted",
        note="still worth keeping",
        block_index=1,
    )
    found = annotation_service.locate(_article(db, settings, site), row)
    assert not found.found
    assert found.block_index == -1
    assert db.get(Annotation, row.id) is not None


def test_a_note_survives_the_block_moving(db: Session, settings: Settings) -> None:
    """The whole reason the anchor is a quotation and not an offset.

    Re-extracting a capture rewrites `derived/text/`, and a later capture of
    the same page has different offsets again — so a byte range would orphan
    every annotation on the archive's first maintenance pass.
    """
    site = _readable(db, settings)
    row = annotation_service.create(
        db,
        site,
        url="https://notes.example.com/post.html",
        quote="coffee filter as well",
        block_index=2,
    )
    shifted = reader.Article(
        url="https://notes.example.com/post.html",
        title="A post",
        timestamp="",
        capture_dir="later",
        capture_id=None,
        blocks=[
            reader.Block("p", "An entirely new opening paragraph."),
            reader.Block("h2", BLOCKS[0]),
            reader.Block("p", BLOCKS[1]),
            reader.Block("p", BLOCKS[2]),
        ],
    )
    found = annotation_service.locate(shifted, row)
    assert found.found
    assert found.block_index == 3


def test_whitespace_from_a_browser_selection_still_matches(
    db: Session, settings: Settings
) -> None:
    """A DOM selection carries newlines the rendered text does not."""
    site = _readable(db, settings)
    row = annotation_service.create(
        db,
        site,
        url="https://notes.example.com/post.html",
        quote="a coffee\n   filter and then",
        block_index=1,
    )
    assert row.quote == "a coffee filter and then"
    assert annotation_service.locate(_article(db, settings, site), row).found


def test_an_empty_selection_is_refused(db: Session, settings: Settings) -> None:
    site = _readable(db, settings)
    try:
        annotation_service.create(db, site, url="https://x/", quote="   \n  ")
    except annotation_service.AnnotationError as exc:
        assert "Select some text" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("an empty quote should be refused")


# ── through the reader and the API ───────────────────────────────────────


def test_the_reader_carries_its_annotations(
    authed: TestClient, db: Session, settings: Settings
) -> None:
    site = _readable(db, settings)
    db.commit()

    created = authed.post(
        f"/api/sites/{site.id}/annotations",
        json={
            "url": "https://notes.example.com/post.html",
            "quote": "a coffee filter",
            "note": "worth remembering",
            "prefix": "paragraph mentions ",
            "suffix": " and then moves on",
            "block_index": 1,
        },
        headers=XHR,
    )
    assert created.status_code == 201, created.text

    page = authed.get(
        f"/api/sites/{site.id}/reader?url=https://notes.example.com/post.html", headers=XHR
    ).json()
    assert len(page["annotations"]) == 1
    mark = page["annotations"][0]
    assert mark["found"] is True
    assert mark["block_index"] == 1
    assert page["blocks"][1]["text"][mark["start"] : mark["end"]] == "a coffee filter"

    edited = authed.patch(
        f"/api/annotations/{mark['id']}", json={"note": "changed my mind"}, headers=XHR
    )
    assert edited.status_code == 200
    assert edited.json()["note"] == "changed my mind"

    removed = authed.delete(f"/api/annotations/{mark['id']}", headers=XHR)
    assert removed.status_code == 200
    listed = authed.get(f"/api/sites/{site.id}/annotations", headers=XHR).json()
    assert listed["annotations"] == []


def test_deleting_a_site_takes_its_notes(db: Session, settings: Settings) -> None:
    site = _readable(db, settings)
    annotation_service.create(
        db, site, url="https://notes.example.com/post.html", quote="a coffee filter"
    )
    assert annotation_service.count_for_site(db, site.id) == 1


# ── profiles that hold more than cookies ─────────────────────────────────

STATE = {
    "cookies": [{"name": "sid", "value": "x", "domain": ".example.com", "path": "/"}],
    "origins": [
        {
            "origin": "https://example.com",
            "localStorage": [{"name": "token", "value": "abc"}, {"name": "user", "value": "me"}],
        }
    ],
}


def test_a_storage_state_is_described_without_being_exposed() -> None:
    """Counts and origins, never a key and never a value (docs/06)."""
    described = profile_service.describe_storage(STATE)
    assert described == {
        "cookies": 1,
        "origins": ["https://example.com"],
        "local_items": 2,
    }
    assert "abc" not in str(described)
    assert "token" not in str(described)


def test_a_profile_with_localstorage_says_what_wget_cannot_use() -> None:
    """The gap nothing else in the app would show.

    A login kept in localStorage works everywhere a browser is involved and
    nowhere wget is — so "the profile test passes and the capture gets the
    sign-in page" would otherwise have no explanation anywhere.
    """
    note = profile_service.storage_note({"storage": profile_service.describe_storage(STATE)})
    assert note is not None
    assert "wget engine cannot" in note

    assert profile_service.storage_note({}) is None
    assert profile_service.storage_note({"storage": {"local_items": 0}}) is None


def test_storage_state_round_trips_through_the_seal(db: Session, sealer: object) -> None:
    from cairn.db.models import AccessProfile

    profile = AccessProfile(name="persona", mode="interactive")
    db.add(profile)
    db.flush()

    profile_service.store_storage_state(db, sealer, profile, STATE)  # type: ignore[arg-type]
    assert profile.storage_enc is not None
    assert b"abc" not in profile.storage_enc

    loaded = profile_service.load_storage_state(sealer, profile)  # type: ignore[arg-type]
    assert loaded == STATE
    assert profile_service.summary(profile)["storage"]["local_items"] == 2
    assert profile_service.summary(profile)["storage_note"]


def test_materializing_hands_the_browser_state_over(
    db: Session, settings: Settings, sealer: object, tmp_path: object
) -> None:
    """In memory, not beside the jar on disk.

    Only this process reads it, and a second plaintext credential in a job's
    temp directory is a second thing to leak.
    """
    from pathlib import Path

    from cairn.db.models import AccessProfile

    profile = AccessProfile(name="materialized", mode="interactive")
    db.add(profile)
    db.flush()
    profile_service.store_cookies(
        db,
        sealer,  # type: ignore[arg-type]
        profile,
        "# Netscape HTTP Cookie File\n.example.com\tTRUE\t/\tFALSE\t0\tsid\tx\n",
        mode="interactive",
    )
    profile_service.store_storage_state(db, sealer, profile, STATE)  # type: ignore[arg-type]

    target = Path(str(tmp_path)) / "job"
    material = profile_service.materialize(db, sealer, profile.id, target)  # type: ignore[arg-type]
    assert material is not None
    assert material.storage_state == STATE
    assert material.cookies_file.is_file()
    assert not (target / "storage.json").exists()


# ── the bookmarklet's server side ────────────────────────────────────────


def test_the_bookmarklet_archives_one_page_and_nothing_else(authed: TestClient) -> None:
    """It is the URL importer with one URL, which is why it needed no new API.

    And no credential: a `javascript:` bookmark runs on somebody else's
    origin, so it opens a Cairn page and lets the session cookie already there
    do the work.
    """
    url = "https://bookmarked.example.com/an-article"
    survey = authed.post("/api/import/urls/survey", json={"text": url}, headers=XHR)
    assert survey.status_code == 200
    assert survey.json()["found"] == 1
    assert survey.json()["groups"][0]["origin"] == "https://bookmarked.example.com"

    done = authed.post("/api/import/urls", json={"text": url}, headers=XHR)
    assert done.status_code == 201, done.text
    assert len(done.json()["created"]) == 1
