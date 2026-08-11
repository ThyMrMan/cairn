"""WACZ export — the archive as one file you can hand to someone.

A [WACZ](https://specs.webrecorder.net/wacz/latest/) is a ZIP holding the
WARCs, an index over them, a page list and a manifest of checksums. Opened in
ReplayWeb.page it replays entirely in the browser with no server at all, which
is what makes it the format for sharing an archive and for an offsite copy
that outlives this tool.

**Written here rather than with py-wacz.** The reference implementation is
`wacz` 0.5.0, and installing it requires `black`, `pytest-cov` and
`frictionless` — the last of which pins `jsonschema==4.17.3` while the engine
registry needs `>=4.23`. Trading a working engine validator for a zip writer
is not a trade worth making, and the format is small: five entries, two of
them one line long. It was written by reading what py-wacz produces, and the
tests hand the result to the pywb already in the image, which unpacks it,
reads this index and serves a page back out of this archive member — an
independent reader resolving our offsets, which is the only property that
makes a WACZ a WACZ.

**Every WARC in the zip must have a unique basename.** The index records
`filename` as a basename and readers resolve it against `archive/`, but every
capture this tool makes writes `part-00000.warc.gz` — so a site-level export
would silently resolve half its index to the wrong file. This is the same
finding as M3's `dir_root`, one layer out.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import zipfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from cairn.config import Settings
from cairn.db.types import to_iso, utcnow
from cairn.logging import get_logger
from cairn.services import replay, storage, textextract

log = get_logger(__name__)

WACZ_VERSION = "1.1.1"
ARCHIVE_DIR = "archive"
INDEX_DIR = "indexes"
PAGES_DIR = "pages"
CDX_NAME = "index.cdx.gz"
IDX_NAME = "index.idx"
PAGES_NAME = "pages.jsonl"
DATAPACKAGE = "datapackage.json"
DIGEST = "datapackage-digest.json"

# Lines per gzip member in the ZipNum index. The point of chunking is that a
# reader can binary-search `index.idx` and inflate one member instead of the
# whole index; 3,000 lines is roughly 300 KB of CDXJ, which is a sensible
# amount to fetch over a range request.
CHUNK_LINES = 3000
COPY_CHUNK = 1024 * 1024


class WaczError(RuntimeError):
    """The export could not be written."""


@dataclass(slots=True)
class Resource:
    name: str
    path: str
    hash: str
    bytes: int

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "path": self.path, "hash": self.hash, "bytes": self.bytes}


@dataclass(slots=True)
class WaczResult:
    path: Path
    warcs: int
    records: int
    pages: int
    size_bytes: int
    resources: list[Resource] = field(default_factory=list)


def export_name(slug: str, when: datetime | None = None) -> str:
    stamp = (when or utcnow()).strftime("%Y%m%dT%H%M%SZ")
    return f"{storage.slugify(slug, fallback='archive')}-{stamp}.wacz"


def exports_dir(settings: Settings, archive_path: str) -> Path:
    return storage.site_dir(settings, archive_path) / storage.EXPORTS_DIR


def build(
    settings: Settings,
    *,
    archive_path: str,
    target: Path,
    capture_dirs: list[str] | None = None,
    title: str = "",
    description: str = "",
    main_page_url: str = "",
    progress: Any = None,
) -> WaczResult:
    """Package a site — or some of its captures — into one `.wacz`.

    Written to a temp name and renamed into place: a half-written export in
    the exports directory would look exactly like a finished one, and the
    thing it is for is being copied somewhere else.
    """
    site_root = storage.site_dir(settings, archive_path)
    sources = _warcs(site_root, capture_dirs)
    if not sources:
        raise WaczError("this archive has no WARC files to export")

    names = _unique_names(site_root, sources)
    lines = _retarget(replay.cdxj_lines(site_root, sources), site_root, names)
    lines.sort()

    pages = _pages(settings, archive_path, lines, capture_dirs)

    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.parent / f".{target.name}.part"
    staging.unlink(missing_ok=True)
    resources: list[Resource] = []
    try:
        with zipfile.ZipFile(staging, "w", allowZip64=True) as zf:
            cdx_blob, idx_blob = _zipnum(lines)
            resources.append(_write(zf, f"{INDEX_DIR}/{CDX_NAME}", cdx_blob, store=True))
            resources.append(_write(zf, f"{INDEX_DIR}/{IDX_NAME}", idx_blob))
            if pages:
                resources.append(_write(zf, f"{PAGES_DIR}/{PAGES_NAME}", _pages_blob(pages)))
            for source in sources:
                name = names[source]
                if progress is not None:
                    progress(name)
                # Already gzipped, and a reader range-reads into them: stored
                # rather than deflated so an offset in the index is an offset
                # in the file.
                resources.append(_copy(zf, f"{ARCHIVE_DIR}/{name}", source))

            package = {
                "profile": "data-package",
                "resources": [r.to_dict() for r in resources],
                "created": to_iso(utcnow()),
                "wacz_version": WACZ_VERSION,
                "software": f"cairn {_version()}",
            }
            if title:
                package["title"] = title
            if description:
                package["description"] = description
            if main_page_url:
                package["mainPageUrl"] = main_page_url
            blob = (json.dumps(package, indent=2) + "\n").encode("utf-8")
            _write(zf, DATAPACKAGE, blob)
            _write(
                zf,
                DIGEST,
                (
                    json.dumps(
                        {"path": DATAPACKAGE, "hash": f"sha256:{hashlib.sha256(blob).hexdigest()}"},
                        indent=2,
                    )
                    + "\n"
                ).encode("utf-8"),
            )
        staging.replace(target)
    except BaseException:
        staging.unlink(missing_ok=True)
        raise

    return WaczResult(
        path=target,
        warcs=len(sources),
        records=len(lines),
        pages=len(pages),
        size_bytes=target.stat().st_size,
        resources=resources,
    )


# ── inputs ───────────────────────────────────────────────────────────────


def _warcs(site_root: Path, capture_dirs: list[str] | None) -> list[Path]:
    captures = site_root / storage.CAPTURES_DIR
    if not captures.is_dir():
        return []
    wanted = set(capture_dirs or [])
    found: list[Path] = []
    for directory in sorted(p for p in captures.iterdir() if p.is_dir()):
        if wanted and directory.name not in wanted:
            continue
        warc_dir = directory / storage.WARC_DIR
        if warc_dir.is_dir():
            found.extend(sorted(warc_dir.glob("*.warc.gz")))
            found.extend(sorted(warc_dir.glob("*.warc")))
    return found


def _unique_names(site_root: Path, sources: Iterable[Path]) -> dict[Path, str]:
    """`<capture>-part-00000.warc.gz`, because a basename is all the index keeps.

    Every capture writes the same filename, so without this a site with two
    captures produces an index in which half the entries point at the other
    capture's file — and the failure is silent, because both files exist and
    both parse.
    """
    names: dict[Path, str] = {}
    taken: set[str] = set()
    for source in sources:
        try:
            capture = source.relative_to(site_root / storage.CAPTURES_DIR).parts[0]
        except ValueError:  # pragma: no cover — sources come from _warcs
            capture = "capture"
        stem, _, suffix = f"{capture}-{source.name}".partition(".")
        candidate = f"{stem}.{suffix}"
        n = 2
        while candidate in taken:
            candidate = f"{stem}-{n}.{suffix}"
            n += 1
        taken.add(candidate)
        names[source] = candidate
    return names


def _retarget(lines: list[str], site_root: Path, names: dict[Path, str]) -> list[str]:
    """Rewrite each CDXJ line's `filename` to its name inside the zip."""
    lookup = {
        str(path.relative_to(site_root)).replace(os.sep, "/"): name for path, name in names.items()
    }

    out: list[str] = []
    for line in lines:
        try:
            surt, timestamp, blob = line.rstrip("\n").split(" ", 2)
            record = json.loads(blob)
        except ValueError:  # pragma: no cover — cdxj-indexer wrote these
            continue
        filename = str(record.get("filename") or "")
        replacement = lookup.get(filename)
        if replacement is None:
            raise WaczError(
                f"the index names {filename!r}, which is not one of the WARCs being packaged. "
                "Every entry has to resolve to a file inside the zip or replay serves nothing."
            )
        record["filename"] = replacement
        out.append(f"{surt} {timestamp} {json.dumps(record)}\n")
    return out


def _pages(
    settings: Settings, archive_path: str, lines: list[str], capture_dirs: list[str] | None
) -> list[dict[str, Any]]:
    """The page list ReplayWeb.page opens with.

    Titles come from the extracted text when it exists, which is the same text
    the search index holds — so a shared archive lists its pages by name
    rather than by URL. Without extraction it still lists the pages.
    """
    titles: dict[str, str] = {}
    for capture_dir in capture_dirs or _capture_dirs(settings, archive_path):
        for page in textextract.read_pages(settings, archive_path, capture_dir):
            if page.title:
                titles.setdefault(page.url, page.title)

    seen: set[str] = set()
    pages: list[dict[str, Any]] = []
    for line in lines:
        try:
            _surt, timestamp, blob = line.rstrip("\n").split(" ", 2)
            record = json.loads(blob)
        except ValueError:  # pragma: no cover
            continue
        mime = str(record.get("mime") or "")
        status = str(record.get("status") or "")
        url = str(record.get("url") or "")
        if "html" not in mime.lower() or not status.startswith("2") or not url or url in seen:
            continue
        seen.add(url)
        pages.append(
            {
                "id": hashlib.blake2b(url.encode("utf-8"), digest_size=8).hexdigest(),
                "url": url,
                "ts": _iso_from_timestamp(timestamp),
                "title": titles.get(url, url),
            }
        )
    return pages


def _capture_dirs(settings: Settings, archive_path: str) -> list[str]:
    captures = storage.site_dir(settings, archive_path) / storage.CAPTURES_DIR
    if not captures.is_dir():
        return []
    return sorted(p.name for p in captures.iterdir() if p.is_dir())


def _iso_from_timestamp(timestamp: str) -> str:
    digits = "".join(ch for ch in timestamp if ch.isdigit())[:14].ljust(14, "0")
    return (
        f"{digits[0:4]}-{digits[4:6]}-{digits[6:8]}T{digits[8:10]}:{digits[10:12]}:{digits[12:14]}Z"
    )


def _pages_blob(pages: list[dict[str, Any]]) -> bytes:
    header = {"format": "json-pages-1.0", "id": "pages", "title": "All Pages"}
    out = io.BytesIO()
    out.write((json.dumps(header) + "\n").encode("utf-8"))
    for page in pages:
        out.write((json.dumps(page, ensure_ascii=False) + "\n").encode("utf-8"))
    return out.getvalue()


# ── the ZipNum index ─────────────────────────────────────────────────────


def _zipnum(lines: list[str]) -> tuple[bytes, bytes]:
    """`index.cdx.gz` as a multi-member gzip, plus the `.idx` that locates them.

    Each chunk is its own gzip member, so a reader that wants one key inflates
    one chunk. A single-member file is a valid special case of the same thing,
    which is what the reference implementation produces for a small archive.
    """
    cdx = io.BytesIO()
    idx = io.BytesIO()
    meta = json.dumps({"format": "cdxj-gzip-1.0", "filename": CDX_NAME})
    idx.write(f"!meta 0 {meta}\n".encode())

    for chunk in _chunks(lines, CHUNK_LINES):
        member = _gzip_member("".join(chunk).encode("utf-8"))
        offset = cdx.tell()
        cdx.write(member)
        first = chunk[0].split(" ", 2)
        idx.write(
            (
                f"{first[0]} {first[1]} "
                + json.dumps(
                    {
                        "offset": offset,
                        "length": len(member),
                        "digest": f"sha256:{hashlib.sha256(member).hexdigest()}",
                    }
                )
                + "\n"
            ).encode("utf-8")
        )
    return cdx.getvalue(), idx.getvalue()


def _chunks(lines: list[str], size: int) -> Iterator[list[str]]:
    for start in range(0, len(lines), size):
        yield lines[start : start + size]


def _gzip_member(payload: bytes) -> bytes:
    buffer = io.BytesIO()
    # mtime=0 so the same archive packaged twice produces the same bytes.
    with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as fh:
        fh.write(payload)
    return buffer.getvalue()


# ── writing ──────────────────────────────────────────────────────────────


def _write(zf: zipfile.ZipFile, name: str, payload: bytes, *, store: bool = False) -> Resource:
    zf.writestr(
        _info(name, store=store),
        payload,
        compress_type=zipfile.ZIP_STORED if store else zipfile.ZIP_DEFLATED,
    )
    return Resource(
        name=name.rsplit("/", 1)[-1],
        path=name,
        hash=f"sha256:{hashlib.sha256(payload).hexdigest()}",
        bytes=len(payload),
    )


def _copy(zf: zipfile.ZipFile, name: str, source: Path) -> Resource:
    """Stream a WARC in, hashing as it goes.

    Never read whole: a site's archive is the large thing here and reading a
    multi-gigabyte WARC into memory to hash it would be the one part of an
    export that could not run on a NAS.
    """
    digest = hashlib.sha256()
    total = 0
    with open(source, "rb") as src, zf.open(_info(name, store=True), "w") as dst:
        while chunk := src.read(COPY_CHUNK):
            digest.update(chunk)
            dst.write(chunk)
            total += len(chunk)
    return Resource(
        name=name.rsplit("/", 1)[-1],
        path=name,
        hash=f"sha256:{digest.hexdigest()}",
        bytes=total,
    )


def _info(name: str, *, store: bool) -> zipfile.ZipInfo:
    # A fixed date so two exports of an unchanged archive differ only where
    # the archive does. ZIP timestamps start at 1980.
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_STORED if store else zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    return info


def _version() -> str:
    from cairn.build import build_info

    info = build_info()
    return f"{info.version} ({info.build})" if info.build else info.version


# ── reading one back ─────────────────────────────────────────────────────


@dataclass(slots=True)
class WaczCheck:
    ok: bool
    problems: list[str] = field(default_factory=list)
    records: int = 0
    resources: int = 0


def verify(path: Path) -> WaczCheck:
    """Re-read an export: checksums, and every index entry resolving.

    The second half is the part that matters. A zip whose checksums agree can
    still be unreplayable, because what makes a WACZ work is that the offset
    the index records lands on the record it claims — which is exactly what a
    reader does and exactly what a basename collision breaks.
    """
    problems: list[str] = []
    try:
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
            for required in (DATAPACKAGE, DIGEST, f"{INDEX_DIR}/{CDX_NAME}"):
                if required not in names:
                    problems.append(f"missing {required}")
            if problems:
                return WaczCheck(ok=False, problems=problems)

            package = json.loads(zf.read(DATAPACKAGE))
            digest = json.loads(zf.read(DIGEST))
            expected = f"sha256:{hashlib.sha256(zf.read(DATAPACKAGE)).hexdigest()}"
            if digest.get("hash") != expected:
                problems.append("datapackage-digest.json does not match datapackage.json")

            resources = package.get("resources") or []
            for resource in resources:
                entry = str(resource.get("path") or "")
                if entry not in names:
                    problems.append(f"{entry} is listed but not in the archive")
                    continue
                raw = zf.read(entry)
                if f"sha256:{hashlib.sha256(raw).hexdigest()}" != resource.get("hash"):
                    problems.append(f"{entry} does not match its checksum")

            records = 0
            for line in _read_cdx(zf).splitlines():
                if not line.strip():
                    continue
                records += 1
                problem = _resolves(zf, line)
                if problem:
                    problems.append(problem)
                    if len(problems) > 20:
                        problems.append("… and more")
                        break
    except (OSError, zipfile.BadZipFile, ValueError) as exc:
        return WaczCheck(ok=False, problems=[f"the file could not be read: {exc}"])

    return WaczCheck(ok=not problems, problems=problems, records=records, resources=len(resources))


def _read_cdx(zf: zipfile.ZipFile) -> str:
    raw = zf.read(f"{INDEX_DIR}/{CDX_NAME}")
    return gzip.decompress(raw).decode("utf-8", errors="replace")


def _resolves(zf: zipfile.ZipFile, line: str) -> str | None:
    from warcio.archiveiterator import ArchiveIterator

    try:
        _surt, _ts, blob = line.split(" ", 2)
        record = json.loads(blob)
        name = f"{ARCHIVE_DIR}/{record['filename']}"
        offset, length = int(record["offset"]), int(record["length"])
    except (KeyError, ValueError):
        return f"unreadable index line: {line[:80]}"

    try:
        with zf.open(name) as fh:
            fh.seek(offset)
            chunk = fh.read(length)
        parsed = next(iter(ArchiveIterator(io.BytesIO(chunk))))
        got = parsed.rec_headers.get_header("WARC-Target-URI")
    # Broad on purpose: an offset landing mid-record is exactly the failure
    # being looked for, and warcio's own exception hierarchy is what it feels
    # like raising that day. Reporting it is the job; propagating it would
    # turn a finding into a 500.
    except Exception as exc:
        return f"{record.get('url', '?')}: the index points at nothing readable ({exc})"

    if got != record.get("url"):
        return f"{record.get('url')}: that offset holds {got!r} instead"
    return None
