"""Importing an ArchiveBox archive, and the metrics endpoint.

The ArchiveBox fixture is built to the schema a real ArchiveBox 0.7.4 wrote —
it was run against a fixture site and its tables read back, rather than
recalled. The columns asserted here are the ones that import depends on, so a
future version that drops or renames one fails here rather than half-importing
somebody's archive.
"""

from __future__ import annotations

import io
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from cairn.config import Settings
from cairn.db.models import Capture, Site
from cairn.services import archivebox, metrics, replay, storage

# Exactly the tables and columns read from a real ArchiveBox 0.7.4.
SCHEMA = """
CREATE TABLE core_snapshot (
    id char(32) NOT NULL PRIMARY KEY,
    timestamp varchar(32) NOT NULL UNIQUE,
    title varchar(512) NULL,
    added datetime NOT NULL,
    updated datetime NULL,
    url varchar(200) NOT NULL
);
CREATE TABLE core_archiveresult (
    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    cmd TEXT NOT NULL,
    pwd varchar(256) NOT NULL,
    cmd_version varchar(128) NULL,
    status varchar(16) NOT NULL,
    output varchar(1024) NOT NULL,
    start_ts datetime NOT NULL,
    end_ts datetime NOT NULL,
    snapshot_id char(32) NOT NULL,
    uuid char(32) NOT NULL,
    extractor varchar(32) NOT NULL
);
CREATE TABLE core_tag (
    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    name varchar(100) NOT NULL UNIQUE,
    slug varchar(100) NOT NULL UNIQUE
);
CREATE TABLE core_snapshot_tags (
    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    snapshot_id char(32) NOT NULL,
    tag_id integer NOT NULL
);
"""

PAGES = {
    "http://coast.test/": b"<html><head><title>Coast</title></head><body>"
    b"<p>UNIQUE-IMPORTED-INDEX</p></body></html>",
    "http://coast.test/post.html": b"<html><head><title>Harris</title></head><body>"
    b"<p>UNIQUE-IMPORTED-POST about the machair in flower.</p></body></html>",
    "http://other.test/thing.html": b"<html><head><title>Other</title></head><body>"
    b"<p>UNIQUE-IMPORTED-OTHER</p></body></html>",
}


def write_warc(path: Path, url: str, body: bytes) -> None:
    from warcio.statusandheaders import StatusAndHeaders
    from warcio.warcwriter import WARCWriter

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as fh:
        writer = WARCWriter(fh, gzip=True)
        headers = StatusAndHeaders(
            "200 OK",
            [("Content-Type", "text/html"), ("Content-Length", str(len(body)))],
            protocol="HTTP/1.1",
        )
        writer.write_record(
            writer.create_warc_record(
                url, "response", payload=io.BytesIO(body), http_headers=headers
            )
        )


@pytest.fixture
def archive(tmp_path: Path) -> Path:
    """An ArchiveBox data directory: two hosts, three snapshots, one tag.

    The third snapshot deliberately has no WARC — ArchiveBox only writes one
    when its wget extractor ran and succeeded, and a real archive is full of
    pages that have a title and a screenshot and nothing else.
    """
    root = tmp_path / "archivebox"
    (root / "archive").mkdir(parents=True)
    (root / "ArchiveBox.conf").write_text("[SERVER_CONFIG]\nVERSION = 0.7.4\n", encoding="utf-8")

    connection = sqlite3.connect(root / archivebox.INDEX_FILE)
    connection.executescript(SCHEMA)

    rows = [
        ("a" * 32, "1786483154.389263", "Coast", "http://coast.test/", True),
        ("b" * 32, "1786483154.389426", "Harris", "http://coast.test/post.html", True),
        ("c" * 32, "1786483160.111111", "Other", "http://other.test/thing.html", True),
        ("d" * 32, "1786483170.222222", "No WARC", "http://coast.test/skipped.html", False),
    ]
    for n, (uid, timestamp, title, url, has_warc) in enumerate(rows):
        connection.execute(
            "INSERT INTO core_snapshot (id, timestamp, title, added, updated, url) "
            "VALUES (?,?,?,?,?,?)",
            (uid, timestamp, title, f"2026-08-11 21:19:1{n}.000000", None, url),
        )
        directory = root / "archive" / timestamp
        directory.mkdir(parents=True, exist_ok=True)
        if has_warc:
            # Named after the moment wget ran, which is not the snapshot's own
            # timestamp — measured on a real archive.
            write_warc(
                directory / "warc" / f"{timestamp.split('.')[0]}.warc.gz",
                url,
                PAGES.get(url, b"<html><body>x</body></html>"),
            )
    connection.execute(
        "INSERT INTO core_tag (id, name, slug) VALUES (1, 'photography', 'photography')"
    )
    connection.execute(
        "INSERT INTO core_snapshot_tags (snapshot_id, tag_id) VALUES (?, 1)", ("a" * 32,)
    )
    connection.commit()
    connection.close()
    return root


# ── reading ──────────────────────────────────────────────────────────────


def test_the_survey_counts_what_can_come_across(archive: Path) -> None:
    result = archivebox.survey(archive)

    assert result.snapshots == 4
    assert result.with_warcs == 3
    assert result.without_warcs == 1
    assert result.hosts == {"coast.test": 2, "other.test": 1}
    assert result.tags == ["photography"]
    assert any("no WARC" in p for p in result.problems)
    # The layout, detected from the tables themselves — ArchiveBox.conf
    # holds a Django SECRET_KEY and no version, checked against a real one.
    assert result.version == "0.6+"


def test_a_directory_that_is_not_an_archivebox_says_so(tmp_path: Path) -> None:
    with pytest.raises(archivebox.ArchiveBoxError, match=r"index\.sqlite3"):
        archivebox.survey(tmp_path)


def test_an_index_with_the_wrong_layout_says_so(tmp_path: Path) -> None:
    """docs/13 warns the layout has shifted across versions. Failing by name
    beats importing half of somebody's archive."""
    root = tmp_path / "old"
    root.mkdir()
    connection = sqlite3.connect(root / archivebox.INDEX_FILE)
    connection.execute("CREATE TABLE links (url TEXT)")
    connection.commit()
    connection.close()

    with pytest.raises(archivebox.ArchiveBoxError, match="core_snapshot"):
        archivebox.survey(root)


def test_the_source_index_is_opened_read_only(archive: Path) -> None:
    """It is somebody's live ArchiveBox database."""
    before = (archive / archivebox.INDEX_FILE).stat().st_mtime_ns
    archivebox.survey(archive)
    archivebox.read_snapshots(archive)
    assert (archive / archivebox.INDEX_FILE).stat().st_mtime_ns == before


# ── importing ────────────────────────────────────────────────────────────


def test_each_domain_becomes_a_site(db: Session, settings: Settings, archive: Path) -> None:
    result = archivebox.import_archive(db, settings, archive)

    assert sorted(result.sites) == ["coast-test", "other-test"]
    assert result.snapshots == 3
    assert result.warcs == 3

    sites = {s.slug: s for s in db.scalars(replay.select(Site)).all()}
    assert set(sites) == {"coast-test", "other-test"}
    assert sites["coast-test"].seed_url == "http://coast.test/"


def test_one_capture_per_site_holding_every_warc(
    db: Session, settings: Settings, archive: Path
) -> None:
    """Not one capture per snapshot: a domain with five hundred archived pages
    would become five hundred captures of one page each."""
    archivebox.import_archive(db, settings, archive)

    site = db.scalars(replay.select(Site).where(Site.slug == "coast-test")).one()
    captures = db.scalars(replay.select(Capture).where(Capture.site_id == site.id)).all()
    assert len(captures) == 1
    capture = captures[0]
    assert capture.kind == "import"
    assert len(capture.warc_files) == 2
    assert all(a["sha256"] for a in capture.warc_files)

    warc_dir = (
        storage.site_dir(settings, site.archive_path)
        / storage.CAPTURES_DIR
        / capture.dir_name
        / storage.WARC_DIR
    )
    names = sorted(p.name for p in warc_dir.iterdir())
    assert len(names) == len(set(names)) == 2


def test_the_imported_archive_is_indexed_and_replayable(
    db: Session, settings: Settings, archive: Path
) -> None:
    """The premise: ArchiveBox already produced per-page WARCs that only need
    indexing, which is what this tool does."""
    archivebox.import_archive(db, settings, archive)
    site = db.scalars(replay.select(Site).where(Site.slug == "coast-test")).one()

    records = replay.lookup(settings, site.archive_path, "http://coast.test/post.html")
    assert records, "the imported WARC is not in the index"

    record = replay.read_record(settings, site.archive_path, records[0])
    assert b"UNIQUE-IMPORTED-POST" in record.content_stream().read()


def test_tags_come_across(db: Session, settings: Settings, archive: Path) -> None:
    from cairn.db.models import SiteTag, Tag

    archivebox.import_archive(db, settings, archive)
    site = db.scalars(replay.select(Site).where(Site.slug == "coast-test")).one()
    names = db.scalars(
        replay.select(Tag.name)
        .join(SiteTag, SiteTag.tag_id == Tag.id)
        .where(SiteTag.site_id == site.id)
    ).all()
    assert list(names) == ["photography"]


def test_the_source_archive_is_left_alone(db: Session, settings: Settings, archive: Path) -> None:
    before = sorted(
        (str(p.relative_to(archive)), p.stat().st_size) for p in archive.rglob("*") if p.is_file()
    )
    archivebox.import_archive(db, settings, archive)
    after = sorted(
        (str(p.relative_to(archive)), p.stat().st_size) for p in archive.rglob("*") if p.is_file()
    )
    assert before == after


def test_one_host_can_be_imported_on_its_own(
    db: Session, settings: Settings, archive: Path
) -> None:
    result = archivebox.import_archive(db, settings, archive, hosts=["other.test"])
    assert result.sites == ["other-test"]
    assert result.snapshots == 1


def test_importing_nothing_says_so(db: Session, settings: Settings, archive: Path) -> None:
    with pytest.raises(archivebox.ArchiveBoxError, match="nothing"):
        archivebox.import_archive(db, settings, archive, hosts=["nowhere.test"])


# ── metrics ──────────────────────────────────────────────────────────────


def test_metrics_are_off_by_default(authed: TestClient) -> None:
    assert authed.get("/api/metrics").status_code == 404


def test_metrics_render_when_enabled(authed: TestClient, db: Session, settings: Settings) -> None:
    from cairn.services import settings_store

    settings_store.put(db, metrics.ENABLED_SETTING, True)
    db.commit()

    res = authed.get("/api/metrics")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/plain")
    body = res.text
    for name in ("cairn_build_info", "cairn_sites", "cairn_captures", "cairn_disk_bytes"):
        assert f"# TYPE {name} gauge" in body
        assert name in body


def test_metrics_carry_no_site_names(
    authed: TestClient, db: Session, settings: Settings, archive: Path
) -> None:
    """A scraper cannot log in, so this endpoint is read by something that may
    be exposed more widely than the app. "Which sites does this person
    archive" must not be in it."""
    from cairn.services import settings_store

    archivebox.import_archive(db, settings, archive)
    settings_store.put(db, metrics.ENABLED_SETTING, True)
    db.commit()

    body = authed.get("/api/metrics").text
    for leak in ("coast", "other.test", "photography", "http://", "Unfiled"):
        assert leak not in body, f"{leak!r} leaked into the metrics"


def test_a_token_is_required_when_set(authed: TestClient, db: Session) -> None:
    from cairn.services import settings_store

    settings_store.put(db, metrics.ENABLED_SETTING, True)
    settings_store.put(db, metrics.TOKEN_SETTING, "s3cret-scrape-token")
    db.commit()

    assert authed.get("/api/metrics").status_code == 401
    assert authed.get("/api/metrics", headers={"Authorization": "Bearer wrong"}).status_code == 401
    ok = authed.get("/api/metrics", headers={"Authorization": "Bearer s3cret-scrape-token"})
    assert ok.status_code == 200


def test_the_exposition_parses_as_prometheus_expects(
    authed: TestClient, db: Session, settings: Settings
) -> None:
    from cairn.services import settings_store

    settings_store.put(db, metrics.ENABLED_SETTING, True)
    db.commit()

    for line in authed.get("/api/metrics").text.splitlines():
        if not line or line.startswith("#"):
            continue
        name, _, value = line.rpartition(" ")
        assert name, line
        float(value)  # every sample line ends in a number
        assert not name.startswith(" ")


def test_metrics_need_no_session(client: TestClient, db: Session) -> None:
    """Prometheus cannot log in. That is the whole reason the endpoint carries
    no names."""
    from cairn.services import settings_store

    settings_store.put(db, metrics.ENABLED_SETTING, True)
    db.commit()
    assert client.get("/api/metrics").status_code == 200
