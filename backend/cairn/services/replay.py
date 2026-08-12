"""The index pywb reads, and the collection tree it discovers.

Replay indexes across many WARCs rather than merging them
([D2](../../../docs/00-decisions.md)); this module owns both halves of what
that needs — a CDXJ spanning every capture of a site, and a directory pywb
can find it through.

Four things were established against pywb 2.9.1 and cdxj-indexer rather than
assumed, and each one changed the design:

  1. **`collections_root` auto-discovery sees collections created after pywb
     started.** A collection linked into the tree while the server is running
     answers on the next request. So adding a site needs no restart, and the
     app never has to reach into the service supervisor — the directory tree
     *is* the interface between them. docs/07 originally specified explicit
     `collections:` entries in the config, which would have meant restarting
     pywb every time a site was added.

  2. **cdxj-indexer records `os.path.basename(filename)` unless `dir_root` is
     passed.** Every capture writes `warc/part-00000.warc.gz`, so without it
     every capture of a site indexes to the same name, and pywb answers 503
     rather than picking one. "Switch between captures of the same page" —
     M3's exit criterion — breaks outright. Verified both ways: two captures
     of one URL resolve to the right bodies with `dir_root`, and 503 without.

  3. **A relative `filename` is what survives a folder move.** It is resolved
     against the collection's `archive` link, which points at the site
     directory, so moving a site between folders only moves the link.

  4. pywb 2.9.1 imports `pkg_resources`, which setuptools 81 removed. The
     image pins below that; see the Dockerfile.
"""

from __future__ import annotations

import io
import json
import os
import shutil
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select

from cairn.config import Settings
from cairn.logging import get_logger
from cairn.services import storage

log = get_logger(__name__)

INDEX_FILE = "site.cdxj"
CONFIG_FILE = "config.yaml"
# pywb's own names for the two directories it looks for inside a collection.
INDEXES_LINK = "indexes"
ARCHIVE_LINK = "archive"

COLLECTION_PREFIX = "site-"


class ReplayError(RuntimeError):
    """The index or the collection tree could not be produced."""


@dataclass(frozen=True, slots=True)
class IndexResult:
    path: Path
    records: int
    warcs: int


def collection_name(site_id: int) -> str:
    """Keyed by ID, never by slug or path.

    Renaming or moving a site must not change its replay URL — bookmarks and
    the iframe's own history depend on it (docs/07).
    """
    return f"{COLLECTION_PREFIX}{site_id}"


def index_path(settings: Settings, archive_path: str) -> Path:
    return storage.site_dir(settings, archive_path) / storage.INDEX_DIR / INDEX_FILE


def site_warcs(settings: Settings, archive_path: str) -> list[Path]:
    """Every WARC of every capture, oldest capture first.

    Sorted so the index is reproducible: the same archive tree indexed twice
    must give byte-identical output, or "did the index change?" is unanswerable.
    """
    captures = storage.site_dir(settings, archive_path) / storage.CAPTURES_DIR
    if not captures.is_dir():
        return []
    return sorted(p for p in captures.glob(f"*/{storage.WARC_DIR}/*.warc.gz") if p.is_file())


def build_index(settings: Settings, archive_path: str) -> IndexResult:
    """Rebuild a site's CDXJ across all of its captures.

    Always a full rebuild, never an append: rebuilds are fast and appending
    invites a whole class of drift bugs where the index and the WARCs disagree
    about a capture that was deleted. Written to a temp file and renamed, so
    replay never reads a half-written index.
    """
    site_root = storage.site_dir(settings, archive_path)
    warcs = site_warcs(settings, archive_path)
    target = index_path(settings, archive_path)
    target.parent.mkdir(parents=True, exist_ok=True)

    if not warcs:
        storage.write_atomic(target, b"")
        return IndexResult(path=target, records=0, warcs=0)

    lines = cdxj_lines(site_root, warcs)
    # A CDXJ line is `<surt> <timestamp> <json>`, and the timestamp is a
    # fixed-width 14 digits, so ordinary string sort is SURT-then-time order —
    # which is what makes "every capture of this URL" a range scan.
    lines.sort()
    # Written as bytes, so a text-mode write never translates the line endings.
    # The index is a portable artefact that travels with the archive tree; a
    # copy written on Windows must be byte-identical to one written in the
    # container, or "rebuild and compare" stops meaning anything.
    storage.write_atomic(target, "".join(lines).encode("utf-8"))
    return IndexResult(path=target, records=len(lines), warcs=len(warcs))


def cdxj_lines(site_root: Path, warcs: list[Path]) -> list[str]:
    """CDXJ for these WARCs, with site-relative filenames.

    Public because the WACZ packager needs exactly this and must not grow a
    second indexer: two implementations of "what is in these WARCs" is how an
    export ends up disagreeing with the replay it was made from.
    """
    try:
        from cdxj_indexer.main import write_cdx_index
    except ImportError as exc:  # pragma: no cover — declared in pyproject
        raise ReplayError("cdxj-indexer is not installed; replay cannot be indexed") from exc

    buffer = io.StringIO()
    relative = [_relative(site_root, warc) for warc in warcs]

    # `dir_root` is not optional — see finding 2 in the module docstring.
    # It is passed as the site root so `filename` comes out site-relative.
    #
    # cdxj-indexer resolves its inputs relative to the process's working
    # directory, and this runs inside the web app, so the paths handed over
    # stay absolute while `dir_root` does the relativising.
    try:
        write_cdx_index(buffer, [str(w) for w in warcs], {"dir_root": str(site_root)})
    # Broad on purpose: a truncated or malformed WARC surfaces here as
    # whatever warcio felt like raising, and none of it should escape as
    # something the caller has to know the indexer's internals to catch.
    except Exception as exc:
        raise ReplayError(f"could not index {len(warcs)} WARC(s): {exc}") from exc

    lines = [
        line + "\n"
        for line in buffer.getvalue().splitlines()
        if line.strip() and not _is_engine_bookkeeping(line)
    ]
    _assert_relative(lines, relative)
    return lines


# wget writes three `metadata://gnu.org/software/wget/warc/…` records into
# every WARC — its manifest, its log and its arguments. They are the crawler
# talking about itself, not anything anybody archived.
_BOOKKEEPING_SCHEMES = ("metadata:", "urn:")


def _is_engine_bookkeeping(line: str) -> bool:
    """Whether a CDXJ line describes the crawler rather than the site.

    Excluded from the index because replay only ever resolves http(s), and
    because the record count is what the UI uses to decide whether a site has
    anything to show. Three bookkeeping records per capture made a site whose
    only real record was a redirect look like a site with four.
    """
    try:
        url = str(json.loads(line.split(" ", 2)[2]).get("url") or "")
    except (IndexError, ValueError):  # pragma: no cover — malformed indexer output
        return False
    return url.lower().startswith(_BOOKKEEPING_SCHEMES)


def _relative(site_root: Path, warc: Path) -> str:
    return str(warc.relative_to(site_root)).replace(os.sep, "/")


def _assert_relative(lines: list[str], expected: list[str]) -> None:
    """Refuse an index whose filenames are bare basenames.

    This is the silent failure from finding 2, and it only shows up on the
    *second* capture of a site — the first has nothing to collide with, so a
    broken index looks perfect until it suddenly serves the wrong page or a
    503. Cheaper to notice here than in a replay bug report.
    """
    allowed = set(expected)
    for line in lines[:1] or []:
        try:
            filename = json.loads(line.split(" ", 2)[2]).get("filename", "")
        except (IndexError, ValueError):  # pragma: no cover — malformed output
            return
        if filename and filename not in allowed:
            raise ReplayError(
                f"the indexer recorded {filename!r}, which is not one of the site-relative "
                "paths it was given. Captures would collide on that name and replay would "
                "serve the wrong one."
            )


# ── reading the index back ───────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class CdxRecord:
    """One archived response, as the index knows it."""

    urlkey: str
    timestamp: str
    url: str
    mime: str | None
    status: str | None
    digest: str | None
    filename: str
    offset: int
    length: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "url": self.url,
            "mime": self.mime,
            "status": self.status,
            "digest": self.digest,
            "filename": self.filename,
            "offset": self.offset,
            "length": self.length,
        }


def surt_key(url: str) -> str:
    """The index key for a URL: a sort-friendly reversed host plus the path."""
    import surt

    return str(surt.surt(url))


def index_records(settings: Settings, archive_path: str) -> Iterator[CdxRecord]:
    """Every record in a site's index, in key order.

    Streamed rather than returned as a list: a large site's index is tens of
    megabytes and both callers — the capture diff and the retention planner —
    read it once and keep only what they need from each line.
    """
    path = index_path(settings, archive_path)
    if not path.is_file():
        return
    with open(path, "rb") as fh:
        for raw in fh:
            record = _parse_line(raw.decode("utf-8", "replace"))
            if record is not None:
                yield record


def lookup(settings: Settings, archive_path: str, url: str) -> list[CdxRecord]:
    """Every capture of one URL, oldest first.

    Read from our own CDXJ rather than through pywb's CDX API, so the app's
    chrome — the capture selector and the version count — still works when
    pywb is down or was never installed. The index is the shared artefact;
    depending on the sidecar to describe it would make one failure look like
    two.
    """
    path = index_path(settings, archive_path)
    if not path.is_file():
        return []
    prefix = f"{surt_key(url)} "

    records: list[CdxRecord] = []
    with open(path, "rb") as fh:
        fh.seek(_first_line_at_or_after(fh, prefix.encode("utf-8")))
        for raw in fh:
            line = raw.decode("utf-8", "replace")
            if not line.startswith(prefix):
                break
            record = _parse_line(line)
            if record is not None:
                records.append(record)
    return records


def _first_line_at_or_after(fh: Any, prefix: bytes) -> int:
    """Byte offset of the first line >= prefix, in a sorted CDXJ.

    The index is sorted and can be tens of megabytes, and this runs on every
    navigation in the replay tab. Scanning linearly would work and would also
    read the whole file each time somebody clicks a link.

    Searching a file of variable-length lines by byte offset works because
    `f(p) = the first complete line at or after p` is monotone: moving p
    forward can only move that line forward. So the predicate `f(p) >= prefix`
    is monotone too, and ordinary bisection applies. `lo` lands somewhere
    inside the line *before* the answer, which is why the tail seeks and then
    discards one line.
    """
    fh.seek(0, os.SEEK_END)
    lo, hi = 0, fh.tell()
    while lo < hi:
        mid = (lo + hi) // 2
        fh.seek(mid)
        if mid:
            fh.readline()  # partial line; the next one is the first whole one
        line = fh.readline()
        if not line or line >= prefix:
            hi = mid
        else:
            lo = mid + 1
    fh.seek(lo)
    if lo:
        fh.readline()
    return int(fh.tell())


def _parse_line(line: str) -> CdxRecord | None:
    try:
        urlkey, timestamp, blob = line.split(" ", 2)
        payload = json.loads(blob)
        return CdxRecord(
            urlkey=urlkey,
            timestamp=timestamp,
            url=str(payload.get("url") or ""),
            mime=payload.get("mime"),
            status=payload.get("status"),
            digest=payload.get("digest"),
            filename=str(payload.get("filename") or ""),
            offset=int(payload.get("offset") or 0),
            length=int(payload.get("length") or 0),
        )
    except (ValueError, TypeError):  # pragma: no cover — a corrupt index line
        return None


def index_stats(settings: Settings, archive_path: str) -> tuple[int, int | None]:
    """Record count and mtime of the index, without parsing it."""
    path = index_path(settings, archive_path)
    if not path.is_file():
        return 0, None
    with open(path, "rb") as fh:
        records = sum(1 for line in fh if line.strip())
    return records, int(path.stat().st_mtime)


def replayable_pages(settings: Settings, archive_path: str) -> int:
    """How many archived records are a page somebody could actually open.

    Records, on their own, do not mean an archive anybody can browse: a
    capture turned away by a content warning holds a redirect and nothing
    else, and one that only ever 404'd holds error pages. Both used to present
    an iframe, which then showed pywb reporting that some URL the person had
    never heard of was not in this collection.
    """
    count = 0
    for record in index_records(settings, archive_path):
        status = str(record.status or "")
        mime = (record.mime or "").lower()
        if status.startswith("2") and ("html" in mime or not mime):
            count += 1
    return count


def read_record(settings: Settings, archive_path: str, record: CdxRecord) -> Any:
    """Fetch one archived response straight out of the WARC by byte range.

    `filename` comes from the index, which is derived from the archive tree —
    but it still reaches the filesystem, so it goes through the same
    containment check as anything else user-influenced.
    """
    from warcio.archiveiterator import ArchiveIterator

    site_root = storage.site_dir(settings, archive_path)
    warc = storage.resolve_within(site_root, record.filename)
    if not warc.is_file():
        raise ReplayError(f"{record.filename} is in the index but not on disk")

    with open(warc, "rb") as fh:
        fh.seek(record.offset)
        for parsed in ArchiveIterator(fh):
            return parsed
    raise ReplayError(f"no record at offset {record.offset} in {record.filename}")


# ── the collection tree pywb discovers ───────────────────────────────────


def collection_dir(settings: Settings, site_id: int) -> Path:
    return settings.collections_dir / collection_name(site_id)


def link_collection(settings: Settings, site_id: int, archive_path: str) -> Path:
    """Point a pywb collection at a site directory.

    Symlinks rather than copies or bind mounts: the index and the WARCs stay
    exactly where the archive tree says they are, and re-pointing a moved site
    is one `unlink` and one `symlink_to`.
    """
    coll = collection_dir(settings, site_id)
    coll.mkdir(parents=True, exist_ok=True)
    site_root = storage.site_dir(settings, archive_path)
    (site_root / storage.INDEX_DIR).mkdir(parents=True, exist_ok=True)

    for name, target in ((INDEXES_LINK, site_root / storage.INDEX_DIR), (ARCHIVE_LINK, site_root)):
        _relink(coll / name, target)
    return coll


def _relink(link: Path, target: Path) -> None:
    if link.is_symlink() or link.exists():
        if link.is_symlink() and Path(os.readlink(link)) == target:
            return
        _remove(link)
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        # Windows needs Developer Mode or elevation for symlinks. The
        # deployment target is Linux and replay is only exercised there, so
        # this degrades to "no replay" rather than failing a capture.
        raise ReplayError(f"could not link {link} -> {target}: {exc}") from exc


def _remove(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def unlink_collection(settings: Settings, site_id: int) -> None:
    coll = collection_dir(settings, site_id)
    if coll.exists() or coll.is_symlink():
        _remove(coll)


def sync_collections(session: Any, settings: Settings) -> tuple[int, int]:
    """Make the tree match the database: link every live site, drop the rest.

    Repair, not the normal path — captures link their own site as they finish.
    This is what fixes a tree after a restore from backup, a folder move, or a
    volume that was recreated, and it is cheap enough to run at every boot.
    """
    from cairn.db.models import Site

    settings.collections_dir.mkdir(parents=True, exist_ok=True)
    sites = session.scalars(select(Site).where(Site.deleted_at.is_(None))).all()

    linked = 0
    wanted: set[str] = set()
    for site in sites:
        wanted.add(collection_name(site.id))
        try:
            link_collection(settings, site.id, site.archive_path)
            linked += 1
        except ReplayError as exc:
            log.warning(
                "could not link replay collection", extra={"site": site.id, "err": str(exc)}
            )

    removed = 0
    for entry in settings.collections_dir.iterdir():
        if entry.name.startswith(COLLECTION_PREFIX) and entry.name not in wanted:
            _remove(entry)
            removed += 1
    return linked, removed


def write_config(settings: Settings) -> Path:
    """Generate pywb's config. Global settings only — collections are the tree.

    Kept minimal on purpose: every key here is one pywb would otherwise
    default differently, and a generated file nobody can hand-edit should not
    be full of restated defaults.
    """
    settings.collections_dir.mkdir(parents=True, exist_ok=True)
    config = {
        # Relative to replay_dir, which is pywb's working directory.
        "collections_root": "collections",
        # Required: pywb serves a top frame containing the rewritten content,
        # which keeps navigation inside the archive and gives the banner a
        # stable place to live (docs/07).
        "framed_replay": True,
        # The app reads its own CDXJ for the version list, but leaving this on
        # keeps pywb's timeline working inside the frame.
        "enable_cdx_api": True,
        "enable_memento": True,
        # pywb injects a policy limiting where archived content can reach.
        "enable_content_security_policy": True,
        "port": settings.replay_port,
    }
    body = (
        "# GENERATED by cairn — do not hand-edit.\n"
        "# Collections are discovered from the collections/ tree, not listed here:\n"
        "# pywb picks up a collection created while it is running, so adding a\n"
        "# site needs no restart.\n" + yaml.safe_dump(config, sort_keys=True)
    )
    target = settings.replay_dir / CONFIG_FILE
    storage.write_atomic(target, body)
    return target
