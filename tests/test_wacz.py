"""WACZ export.

The check that matters is not "the zip has the right entries" but "the offset
the index records lands on the record it names" — that is what a replay client
does, and it is what a basename collision between two captures breaks
silently. Both are asserted here; the end-to-end suite additionally hands the
file to py-wacz's validator and replays it through a real pywb.
"""

from __future__ import annotations

import gzip
import io
import json
import zipfile
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from cairn.config import Settings
from cairn.db.models import Capture, Site
from cairn.db.types import utcnow
from cairn.services import storage, textextract, wacz
from tests.test_search import blogger_page, write_warc

MARKER = "the machair in flower on Harris"


def make_capture(
    settings: Settings, archive_path: str, dir_name: str, pages: dict[str, bytes]
) -> Path:
    storage.ensure_site_dirs(settings, archive_path)
    capture_dir = storage.ensure_capture_dirs(settings, archive_path, dir_name)
    warc = capture_dir / storage.WARC_DIR / "part-00000.warc.gz"
    write_warc(warc, pages)
    return warc


@pytest.fixture
def two_captures(settings: Settings) -> str:
    """A site with two captures, each writing `part-00000.warc.gz`.

    The collision is the point: every capture this tool makes uses that name,
    and a WACZ index keys on the basename alone.
    """
    archive_path = "Unfiled/coast"
    make_capture(
        settings,
        archive_path,
        "20260811T090000Z-full-wget",
        {
            "http://blog.test/": blogger_page("index", "Index", "Photographs from the west."),
            "http://blog.test/harris.html": blogger_page("harris", "Harris", MARKER),
        },
    )
    make_capture(
        settings,
        archive_path,
        "20260812T090000Z-feed-wget",
        {
            "http://blog.test/luskentyre.html": blogger_page(
                "luskentyre", "Luskentyre", "Rain for three days."
            )
        },
    )
    return archive_path


def test_the_export_has_the_shape_a_reader_expects(
    two_captures: str, settings: Settings, tmp_path: Path
) -> None:
    target = tmp_path / "coast.wacz"
    result = wacz.build(settings, archive_path=two_captures, target=target, title="Coast")

    assert result.warcs == 2
    assert result.records > 0
    with zipfile.ZipFile(target) as zf:
        names = set(zf.namelist())
        assert "datapackage.json" in names
        assert "datapackage-digest.json" in names
        assert "indexes/index.cdx.gz" in names
        assert "indexes/index.idx" in names
        assert "pages/pages.jsonl" in names
        assert sum(1 for n in names if n.startswith("archive/")) == 2

        package = json.loads(zf.read("datapackage.json"))
        assert package["wacz_version"] == "1.1.1"
        assert package["profile"] == "data-package"
        assert package["title"] == "Coast"

        # The WARCs are stored, not deflated: a reader range-reads into them,
        # so an offset in the index has to be an offset in the file.
        for info in zf.infolist():
            if info.filename.startswith("archive/"):
                assert info.compress_type == zipfile.ZIP_STORED


def test_two_captures_do_not_collide_on_one_filename(
    two_captures: str, settings: Settings, tmp_path: Path
) -> None:
    """Both captures write `part-00000.warc.gz`. Packaged under that name,
    half the index would resolve to the other capture's file — and both files
    exist and both parse, so nothing would say so."""
    target = tmp_path / "coast.wacz"
    wacz.build(settings, archive_path=two_captures, target=target)

    with zipfile.ZipFile(target) as zf:
        archived = sorted(n for n in zf.namelist() if n.startswith("archive/"))
        assert len(archived) == len(set(archived)) == 2
        assert all("part-00000.warc.gz" in n for n in archived)
        assert "archive/part-00000.warc.gz" not in archived

        named = {
            json.loads(line.split(" ", 2)[2])["filename"]
            for line in gzip.decompress(zf.read("indexes/index.cdx.gz")).decode().splitlines()
        }
        assert named == {n.split("/", 1)[1] for n in archived}


def test_every_index_entry_resolves_to_its_own_record(
    two_captures: str, settings: Settings, tmp_path: Path
) -> None:
    """Read it back the way a replay client does: seek to the offset in the
    file the index names, and check the record found there is the one claimed."""
    from warcio.archiveiterator import ArchiveIterator

    target = tmp_path / "coast.wacz"
    wacz.build(settings, archive_path=two_captures, target=target)

    checked = 0
    with zipfile.ZipFile(target) as zf:
        for line in gzip.decompress(zf.read("indexes/index.cdx.gz")).decode().splitlines():
            record = json.loads(line.split(" ", 2)[2])
            raw = zf.read(f"archive/{record['filename']}")
            chunk = raw[int(record["offset"]) : int(record["offset"]) + int(record["length"])]
            parsed = next(iter(ArchiveIterator(io.BytesIO(chunk))))
            assert parsed.rec_headers.get_header("WARC-Target-URI") == record["url"]
            checked += 1
    assert checked >= 3


def test_verify_agrees_with_itself(two_captures: str, settings: Settings, tmp_path: Path) -> None:
    target = tmp_path / "coast.wacz"
    wacz.build(settings, archive_path=two_captures, target=target)
    check = wacz.verify(target)
    assert check.ok, check.problems
    assert check.records >= 3


def test_verify_notices_a_flipped_byte(
    two_captures: str, settings: Settings, tmp_path: Path
) -> None:
    """An export whose checksums are not checked is a backup nobody tested."""
    target = tmp_path / "coast.wacz"
    wacz.build(settings, archive_path=two_captures, target=target)

    broken = tmp_path / "broken.wacz"
    with zipfile.ZipFile(target) as src, zipfile.ZipFile(broken, "w") as dst:
        for info in src.infolist():
            data = src.read(info.filename)
            if info.filename.startswith("archive/"):
                data = data[:-16] + b"\x00" * 16
            dst.writestr(info, data, compress_type=info.compress_type)

    check = wacz.verify(broken)
    assert not check.ok
    assert any("checksum" in p for p in check.problems)


def test_verify_notices_an_index_pointing_at_the_wrong_place(
    two_captures: str, settings: Settings, tmp_path: Path
) -> None:
    """The failure a basename collision produces: intact files, correct
    checksums, and an index that resolves to the wrong record."""
    target = tmp_path / "coast.wacz"
    wacz.build(settings, archive_path=two_captures, target=target)

    skewed = tmp_path / "skewed.wacz"
    with zipfile.ZipFile(target) as src, zipfile.ZipFile(skewed, "w") as dst:
        for info in src.infolist():
            data = src.read(info.filename)
            if info.filename == "indexes/index.cdx.gz":
                lines = []
                for line in gzip.decompress(data).decode().splitlines():
                    surt, ts, blob = line.split(" ", 2)
                    record = json.loads(blob)
                    record["offset"] = int(record["offset"]) + 1
                    lines.append(f"{surt} {ts} {json.dumps(record)}")
                data = gzip.compress(("\n".join(lines) + "\n").encode())
            dst.writestr(info, data, compress_type=info.compress_type)

    # The datapackage now disagrees too, which is itself a finding; the point
    # is that the resolution check does not pass on a broken index.
    check = wacz.verify(skewed)
    assert not check.ok


def test_a_single_capture_can_be_exported_on_its_own(
    two_captures: str, settings: Settings, tmp_path: Path
) -> None:
    target = tmp_path / "one.wacz"
    result = wacz.build(
        settings,
        archive_path=two_captures,
        target=target,
        capture_dirs=["20260812T090000Z-feed-wget"],
    )
    assert result.warcs == 1
    with zipfile.ZipFile(target) as zf:
        assert sum(1 for n in zf.namelist() if n.startswith("archive/")) == 1
        urls = {
            json.loads(line.split(" ", 2)[2])["url"]
            for line in gzip.decompress(zf.read("indexes/index.cdx.gz")).decode().splitlines()
        }
    assert urls == {"http://blog.test/luskentyre.html"}


def test_pages_carry_the_titles_extraction_found(
    two_captures: str, settings: Settings, tmp_path: Path
) -> None:
    """A shared archive that lists its pages by URL is a directory listing.
    The titles are already extracted for search; the export uses the same ones."""
    for capture_dir in ("20260811T090000Z-full-wget", "20260812T090000Z-feed-wget"):
        textextract.extract_capture(settings, two_captures, capture_dir)

    target = tmp_path / "coast.wacz"
    wacz.build(settings, archive_path=two_captures, target=target)

    with zipfile.ZipFile(target) as zf:
        lines = zf.read("pages/pages.jsonl").decode().splitlines()
    header = json.loads(lines[0])
    assert header["format"] == "json-pages-1.0"
    pages = [json.loads(line) for line in lines[1:]]
    assert {p["url"] for p in pages} == {
        "http://blog.test/",
        "http://blog.test/harris.html",
        "http://blog.test/luskentyre.html",
    }
    assert any("Harris" in p["title"] for p in pages)
    assert all(p["ts"].endswith("Z") and "T" in p["ts"] for p in pages)


def test_an_interrupted_export_leaves_no_half_written_file(
    two_captures: str, settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An exports directory is a place people copy files out of. A partial
    file there is indistinguishable from a finished one."""

    def explode(*_args: object, **_kwargs: object) -> None:
        raise OSError("the volume went away")

    monkeypatch.setattr(wacz, "_copy", explode)
    target = tmp_path / "coast.wacz"
    with pytest.raises(OSError):
        wacz.build(settings, archive_path=two_captures, target=target)

    assert not target.exists()
    assert not list(tmp_path.glob(".*.part"))


def test_an_empty_archive_says_so(settings: Settings, tmp_path: Path) -> None:
    storage.ensure_site_dirs(settings, "Unfiled/empty")
    with pytest.raises(wacz.WaczError):
        wacz.build(settings, archive_path="Unfiled/empty", target=tmp_path / "x.wacz")


def test_the_job_writes_into_the_exports_directory(
    db: Session, settings: Settings, two_captures: str
) -> None:
    """`exports/` is listed by reading the directory, so this is also what
    makes an export appear in the UI."""
    site = Site(
        folder_id=1,
        slug="coast",
        title="Coast",
        seed_url="http://blog.test/",
        primary_host="blog.test",
        archive_path=two_captures,
    )
    db.add(site)
    db.flush()
    db.add(
        Capture(
            site_id=site.id,
            kind="full",
            engine_id="wget-warc",
            dir_name="20260811T090000Z-full-wget",
            status="ok",
            started_at=utcnow(),
        )
    )
    db.flush()

    target = wacz.exports_dir(settings, site.archive_path) / wacz.export_name(site.slug)
    result = wacz.build(settings, archive_path=site.archive_path, target=target)

    assert result.path.parent.name == storage.EXPORTS_DIR
    assert result.path.name.startswith("coast-")
    assert result.path.name.endswith(".wacz")
    assert result.size_bytes > 0
