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

import dataclasses
import functools
import heapq
import io
import json
import os
import re
import shutil
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

import yaml
from sqlalchemy import select

from cairn.config import Settings
from cairn.logging import get_logger
from cairn.services import storage

log = get_logger(__name__)

INDEX_FILE = "site.cdxj"
# What the index holds, WARC by WARC, so a capture can add its own records
# without re-reading everybody else's (`update_index`). Beside the index
# because it describes that file and nothing else. pywb loads only names
# ending in .cdx, .cdxj, .idx or .summary from an index directory — read off
# 2.9.1's `BaseDirectoryIndexSource` — so this one is invisible to it.
INDEX_STATE_FILE = "site.cdxj.json"
# Bumped whenever the lines an index holds for a given WARC would change — a
# new filter here, a different way of calling the indexer — so that every
# index is rebuilt once rather than keeping lines a rebuild would not write.
INDEX_FORMAT = 1
CONFIG_FILE = "config.yaml"
# pywb's own names for the two directories it looks for inside a collection.
INDEXES_LINK = "indexes"
ARCHIVE_LINK = "archive"

COLLECTION_PREFIX = "site-"

# pywb's `templates_dir` default, relative to its working directory.
TEMPLATES_DIR = "templates"
# Deliberately *not* called head_insert.html. pywb resolves templates through a
# ChoiceLoader over this directory and then its own package, so a file with a
# different name can `{% include "head_insert.html" %}` and get pywb's original
# rather than recursing into itself.
#
# That indirection is the whole design. Overriding head_insert.html outright
# would mean carrying a copy of pywb's, which is version-coupled to wombat's
# bootstrap — and a pywb upgrade would then silently serve pages with the URL
# rewriting gone, which looks fine until every link reaches the live site.
# Measured against pywb 2.9.1: with the include, pywb's own insert is still
# present and ours is added.
HEAD_INSERT_FILE = "cairn_head_insert.html"


class ReplayError(RuntimeError):
    """The index or the collection tree could not be produced."""


@dataclass(frozen=True, slots=True)
class IndexResult:
    path: Path
    records: int
    warcs: int
    #: Records present in the WARCs and deliberately left out of the index.
    #: See `build_index`.
    withheld: int = 0
    #: WARCs read to produce this index: every one for a rebuild, only the
    #: new or changed ones for an update.
    read: int = 0
    #: Why the index was rebuilt from scratch; None when it was updated.
    rebuilt: str | None = None


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


def build_index(
    settings: Settings, archive_path: str, *, withhold: list[str] | None = None
) -> IndexResult:
    """Rebuild a site's CDXJ from every WARC it has.

    The repair: what Rebuild index and `cairn reindex` do, and what
    `update_index` falls back to whenever it cannot vouch for its own record
    of what the index holds. Written to a temp file and renamed, so replay
    never reads a half-written index.

    **`withhold` keeps a recorded URL out of replay without touching the
    archive.** Some things get into a WARC despite the scope rejecting them,
    because no crawler flag can stop them. Measured on a real Blogger blog:
    the content warning is an *iframe* — Blogger returns it with
    `content-security-policy: frame-ancestors <the blog>` — so it is a frame
    navigation, and browsertrix exempts page navigation from `--blockRules`
    while `--exclude` only ever filtered the crawl queue. The result was 149
    archived warnings replaying on top of 351 correctly archived posts, with
    the reject patterns naming them exactly and unable to do anything.

    Dropping the *index* entry is the lever that does work. The frame fails to
    resolve, the real page underneath is what the reader sees, and the bytes
    stay in the WARC — which matters, because a WARC is immutable
    ([D2](../../../docs/00-decisions.md)) and "we deleted it for you" is not a
    property an archive should have. Rebuild without the filter and every
    record is back.

    Deliberately not applied to `cdxj_lines`, which the WACZ packager shares.
    An export is the archive, and withholding is a statement about *this
    instance's replay* rather than about what was captured.
    """
    patterns = _normalised(withhold)
    target = index_path(settings, archive_path)
    with _exclusive(target):
        return _rebuild(settings, archive_path, target, patterns, "a rebuild was asked for")


def update_index(
    settings: Settings, archive_path: str, *, withhold: list[str] | None = None
) -> IndexResult:
    """Bring a site's CDXJ up to date, reading only the WARCs it has not seen.

    What post-processing does after every capture. It used to rebuild from
    every WARC the site had: seconds for a small blog, minutes for a large
    one. The indexer reads about 323 MB/s on a fast local disk, so a site with
    50 GB of WARCs re-read all of it to add a feed capture of fifteen
    megabytes — twice a day, for every blog on a feed.

    `index/site.cdxj.json` records, for each WARC the index covers, its size,
    its mtime and what it contributed. An update compares that with the WARCs
    on disk: new ones are read, changed ones re-read, missing ones dropped,
    and the fresh lines merged into the existing sorted file. The result is
    byte-identical to `build_index` over the same tree. That is what answers
    the argument this function used to be written against — that an index
    which is appended to drifts from the archive it describes.

    Whatever it cannot vouch for is a full rebuild instead: no record, a
    record from another format or indexer version, different skip patterns,
    or an index whose size and mtime are not the ones recorded when it was
    written.
    """
    patterns = _normalised(withhold)
    target = index_path(settings, archive_path)
    with _exclusive(target):
        state = _read_state(target)
        reason = _why_rebuild(state, target, patterns)
        if reason is not None or state is None:
            return _rebuild(settings, archive_path, target, patterns, reason or "no record")
        return _update(settings, archive_path, target, state, patterns)


def forget_capture(settings: Settings, archive_path: str, dir_name: str) -> int:
    """Take a deleted capture's records out of the index; return how many.

    Reads no WARC — every record names its file, and a capture's files all
    live under one directory — so this is cheap enough to run inside the
    request that deleted the capture. Without it the index went on naming
    files that were gone until the site's next capture, and replay answered
    503 for every one of them.
    """
    target = index_path(settings, archive_path)
    prefix = f"{storage.CAPTURES_DIR}/{dir_name}/"
    with _exclusive(target):
        if not target.is_file():
            return 0
        state = _read_state(target)
        trusted = state is not None and _why_rebuild(state, target, None) is None
        # The prefix as the indexer wrote it: opening quote, JSON escaping, no
        # closing quote. Only a line containing that is worth parsing.
        marker = json.dumps(prefix)[:-1]
        dropped = 0
        # The reader is closed before the writer renames over it; see
        # `storage.atomic_writer`.
        with storage.atomic_writer(target) as out, open(target, "rb") as current:
            for raw in current:
                line = raw.decode("utf-8")
                if marker in line and _filename_of(line).startswith(prefix):
                    dropped += 1
                    continue
                out.write(raw)
        if trusted and state is not None:
            files = {n: w for n, w in _recorded(state).items() if not n.startswith(prefix)}
            _write_state(target, list(state.get("withhold") or []), files)
        return dropped


def _rebuild(
    settings: Settings, archive_path: str, target: Path, patterns: list[str], reason: str
) -> IndexResult:
    log.info("rebuilding a replay index", extra={"index": str(target), "why": reason})
    site_root = storage.site_dir(settings, archive_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    compiled = _compile(patterns)

    lines: list[str] = []
    files: dict[str, _Warc] = {}
    warcs = site_warcs(settings, archive_path)
    for warc in warcs:
        # Before reading, so a WARC still growing is recorded smaller than it
        # ends up, and the next update reads it again.
        size, mtime_ns = _stat(warc)
        kept, withheld = _index_warc(site_root, warc, compiled)
        lines.extend(kept)
        files[_relative(site_root, warc)] = _Warc(size, mtime_ns, len(kept), withheld)

    # A CDXJ line is `<surt> <timestamp> <json>`, and the timestamp is a
    # fixed-width 14 digits, so ordinary string sort is SURT-then-time order —
    # which is what makes "every capture of this URL" a range scan.
    lines.sort()
    # Written as bytes, so a text-mode write never translates the line endings.
    # The index is a portable artefact that travels with the archive tree; a
    # copy written on Windows must be byte-identical to one written in the
    # container, or "rebuild and compare" stops meaning anything.
    storage.write_atomic(target, "".join(lines).encode("utf-8"))
    _write_state(target, patterns, files)
    return _result(target, files, len(warcs), read=len(warcs), rebuilt=reason)


def _update(
    settings: Settings,
    archive_path: str,
    target: Path,
    state: dict[str, Any],
    patterns: list[str],
) -> IndexResult:
    site_root = storage.site_dir(settings, archive_path)
    recorded = _recorded(state)
    on_disk = {_relative(site_root, warc): warc for warc in site_warcs(settings, archive_path)}
    seen = {name: _stat(warc) for name, warc in on_disk.items()}

    gone = recorded.keys() - on_disk.keys()
    changed = {
        name
        for name in recorded.keys() & on_disk.keys()
        if seen[name] != (recorded[name].size, recorded[name].mtime_ns)
    }
    new = on_disk.keys() - recorded.keys()
    files = {name: w for name, w in recorded.items() if name not in gone and name not in changed}
    if not (gone or changed or new):
        return _result(target, files, len(on_disk), read=0)

    compiled = _compile(patterns)
    fresh: list[str] = []
    for name in sorted(new | changed):
        kept, withheld = _index_warc(site_root, on_disk[name], compiled)
        fresh.extend(kept)
        files[name] = _Warc(*seen[name], records=len(kept), withheld=withheld)
    fresh.sort()

    # Both inputs are in plain string order, so a merge of the two is the
    # order a rebuild's single sort produces — and the bytes are the same.
    with storage.atomic_writer(target) as out, open(target, "rb") as current:
        for line in heapq.merge(_lines_except(current, gone | changed), fresh):
            out.write(line.encode("utf-8"))
    _write_state(target, patterns, files)
    return _result(target, files, len(on_disk), read=len(new | changed))


def _result(
    target: Path, files: dict[str, _Warc], warcs: int, *, read: int, rebuilt: str | None = None
) -> IndexResult:
    return IndexResult(
        path=target,
        records=sum(w.records for w in files.values()),
        warcs=warcs,
        withheld=sum(w.withheld for w in files.values()),
        read=read,
        rebuilt=rebuilt,
    )


def _index_warc(
    site_root: Path, warc: Path, compiled: list[re.Pattern[str]]
) -> tuple[list[str], int]:
    """One WARC's lines, and how many of its records were withheld.

    One WARC at a time, including in a rebuild. cdxj-indexer keeps no state
    from one input file to the next — each gets its own record iterator — so
    a file's lines are the same whatever else is indexed beside it, which is
    the property an update depends on.
    """
    lines = cdxj_lines(site_root, [warc])
    if not compiled:
        return lines, 0
    return _drop_matching(lines, compiled)


def _lines_except(current: BinaryIO, files: set[str]) -> Iterator[str]:
    """An index's lines, minus those naming any of `files`."""
    # Each filename as the indexer wrote it, quotes and escaping included, so
    # most lines are ruled out without parsing any JSON.
    markers = [json.dumps(name) for name in files]
    for raw in current:
        line = raw.decode("utf-8")
        if markers and any(m in line for m in markers) and _filename_of(line) in files:
            continue
        yield line


def _filename_of(line: str) -> str:
    try:
        return str(json.loads(line.split(" ", 2)[2]).get("filename") or "")
    except (IndexError, ValueError, AttributeError):
        return ""


# ── what the index holds ─────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _Warc:
    """One WARC as the index holds it."""

    size: int
    mtime_ns: int
    records: int
    withheld: int


_EXCLUSIVE: dict[str, threading.Lock] = {}
_EXCLUSIVE_GUARD = threading.Lock()


@contextmanager
def _exclusive(index: Path) -> Iterator[None]:
    """One writer per index in this process.

    Post-processing runs in a worker thread and Rebuild index in a request
    thread, and an update that read the index while a rebuild replaced it
    would merge into a file that no longer exists. `cairn reindex` runs in a
    process of its own and is not covered — it is a repair somebody starts by
    hand, and the worst it can race into is a record that no longer matches,
    which the next update treats as a reason to rebuild.
    """
    with _EXCLUSIVE_GUARD:
        lock = _EXCLUSIVE.setdefault(str(index), threading.Lock())
    with lock:
        yield


def _normalised(withhold: list[str] | None) -> list[str]:
    # The filter drops a line if any pattern matches, so order and repeats
    # mean nothing — and must not read as a change.
    return sorted(set(withhold or []))


@functools.cache
def _indexer_version() -> str:
    from importlib import metadata

    try:
        return metadata.version("cdxj-indexer")
    except metadata.PackageNotFoundError:  # pragma: no cover — declared in pyproject
        return "unknown"


def _stat(path: Path) -> tuple[int, int]:
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns


def _state_path(index: Path) -> Path:
    return index.with_name(INDEX_STATE_FILE)


def _read_state(index: Path) -> dict[str, Any] | None:
    try:
        state = json.loads(_state_path(index).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return state if isinstance(state, dict) else None


def _recorded(state: dict[str, Any]) -> dict[str, _Warc]:
    return {str(name): _Warc(**entry) for name, entry in state["warcs"].items()}


def _write_state(index: Path, patterns: list[str], files: dict[str, _Warc]) -> None:
    """Record what `index` holds.

    Written after the index and describing it by size and mtime, so a crash
    between the two leaves a record that no longer matches the file — which
    is a rebuild, never an update against the wrong one.
    """
    size, mtime_ns = _stat(index)
    payload = {
        "format": INDEX_FORMAT,
        "indexer": _indexer_version(),
        "withhold": patterns,
        "index": {"size": size, "mtime_ns": mtime_ns},
        "warcs": {name: dataclasses.asdict(files[name]) for name in sorted(files)},
    }
    storage.write_atomic(_state_path(index), (json.dumps(payload, indent=1) + "\n").encode("utf-8"))


def _why_rebuild(
    state: dict[str, Any] | None, index: Path, patterns: list[str] | None
) -> str | None:
    """Why the record cannot be trusted to update `index`, or None.

    `patterns` None skips the skip-pattern check, for callers that only ever
    take records out.
    """
    if not index.is_file():
        return "there was no index"
    if state is None:
        return "nothing recorded which WARCs the index holds"
    if state.get("format") != INDEX_FORMAT:
        return "the index format changed"
    if state.get("indexer") != _indexer_version():
        return "cdxj-indexer changed"
    if patterns is not None and state.get("withhold") != patterns:
        return "the skip patterns changed"
    described = state.get("index")
    if not isinstance(described, dict):
        return "the record does not describe an index"
    if (described.get("size"), described.get("mtime_ns")) != _stat(index):
        return "the index was rewritten by something that did not record it"
    try:
        _recorded(state)
    except (AttributeError, KeyError, TypeError):
        return "the record of indexed WARCs is unreadable"
    return None


def withheld_patterns(session: Any, site: Any) -> list[str]:
    """What this site keeps out of replay: the reject patterns that apply to it.

    Its own and the instance-wide skip list's, which mean the same thing and
    so are read the same way — a rule typed once in Settings would be a
    strange thing to have decide the crawl and not the index.

    The scope's explicit list only — what a person or a preset actually asked
    to skip. Not `build_reject_patterns`, whose generated asset fences are
    about *what to fetch*: withholding by those would drop an image whose URL
    happens not to end in a known extension, and a missing photo is a worse
    outcome than an archived content warning.

    Used by every caller of `build_index`, so a manual rebuild cannot quietly
    restore what a capture withheld.

    **Minus whatever a companion pass lifts**, and without that the pass is
    self-defeating: the lean Blogger preset rejects the pagination trail so the
    expensive crawl skips it, the pass fetches exactly those URLs, and this
    function then hid all 68 of them behind the same patterns. Measured that
    way — the pass reported 69 URLs fetched and the index reported 68 more
    records withheld, which is every one of them but the home page.

    The two rules read alike and are not. "Do not spend crawl time on this" is
    about cost; "do not serve this" is about what the archive shows. They
    coincide for a content-warning iframe, which is what this was written for,
    and they are opposites for a trail somebody deliberately went and fetched.
    """
    from cairn.services import discovery_service
    from cairn.services import sites as site_service

    try:
        patterns = list(site_service.resolved_scope(session, site).all_reject_patterns)
    except Exception:  # pragma: no cover — a site mid-delete
        return []

    companion = discovery_service.companion_pass_for(site)
    if companion is None:
        return patterns
    # Unconditionally, rather than only once the pass has run: a site whose
    # pass is still pending has no such records to serve, so lifting early
    # changes nothing, and keying this off capture history would make the
    # index depend on which order two captures happened in.
    lifted = set(companion.lifts_rejects)
    return [p for p in patterns if p not in lifted]


def _without(lines: list[str], patterns: list[str]) -> tuple[list[str], int]:
    """Drop the index lines whose URL matches any pattern.

    Matched against the record's own `url`, not the SURT key at the front of
    the line. The SURT is canonicalised — host reversed, case folded, some
    parameters reordered — so a pattern a person wrote against the URL they
    saw in a fetch list would match it only by accident.
    """
    return _drop_matching(lines, _compile(patterns))


def _compile(patterns: list[str]) -> list[re.Pattern[str]]:
    """A pattern that will not compile is skipped rather than fatal.

    These come from a scope somebody typed into, and one bad character should
    not cost the site its whole replay index.
    """
    compiled = []
    for pattern in patterns:
        try:
            compiled.append(re.compile(pattern))
        except re.error as exc:
            log.warning("skipping an unusable withhold pattern", extra={"err": str(exc)})
    return compiled


def _drop_matching(lines: list[str], compiled: list[re.Pattern[str]]) -> tuple[list[str], int]:
    if not compiled:
        return lines, 0
    keep: list[str] = []
    dropped = 0
    for line in lines:
        parts = line.split(" ", 2)
        url = ""
        if len(parts) == 3:
            try:
                url = str(json.loads(parts[2]).get("url") or "")
            except ValueError:  # pragma: no cover — the indexer writes JSON
                url = ""
        if url and any(p.search(url) for p in compiled):
            dropped += 1
            continue
        keep.append(line)
    return keep, dropped


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
        what = relative[0] if len(relative) == 1 else f"{len(warcs)} WARC(s)"
        raise ReplayError(f"could not index {what}: {exc}") from exc

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


# Line counts keyed by (path, mtime, size). Counting means reading the file,
# and on a large archive that is a hundred megabytes off an array — for a
# number that cannot change until a capture rewrites the index, at which point
# both mtime and size do. Bounded because an instance can hold many sites.
_COUNT_CACHE: dict[str, tuple[float, int, int]] = {}
_COUNT_CACHE_MAX = 512


def index_stats(settings: Settings, archive_path: str) -> tuple[int, int | None]:
    """Record count and mtime of the index, without parsing it."""
    path = index_path(settings, archive_path)
    if not path.is_file():
        return 0, None

    stat = path.stat()
    key = str(path)
    cached = _COUNT_CACHE.get(key)
    if cached is not None and cached[0] == stat.st_mtime and cached[1] == stat.st_size:
        return cached[2], int(stat.st_mtime)

    with open(path, "rb") as fh:
        records = sum(1 for line in fh if line.strip())

    if len(_COUNT_CACHE) >= _COUNT_CACHE_MAX:
        _COUNT_CACHE.clear()
    _COUNT_CACHE[key] = (stat.st_mtime, stat.st_size, records)
    return records, int(stat.st_mtime)


def _is_replayable_page(record: CdxRecord) -> bool:
    status = str(record.status or "")
    mime = (record.mime or "").lower()
    return status.startswith("2") and ("html" in mime or not mime)


def replayable_pages(settings: Settings, archive_path: str, *, limit: int | None = None) -> int:
    """How many archived records are a page somebody could actually open.

    Records, on their own, do not mean an archive anybody can browse: a
    capture turned away by a content warning holds a redirect and nothing
    else, and one that only ever 404'd holds error pages. Both used to present
    an iframe, which then showed pywb reporting that some URL the person had
    never heard of was not in this collection.

    `limit` stops early once that many have been found. Every caller so far
    wants to know whether the number is zero, and counting the rest is not
    free: this parses the index line by line, which on a 500,000-record
    archive measured **1,435 ms** against 3 ms for a thousand records — paid
    on every open of the replay tab, from an array where the file is not in
    cache. `has_replayable_page` is the form to reach for.
    """
    count = 0
    for record in index_records(settings, archive_path):
        if _is_replayable_page(record):
            count += 1
            if limit is not None and count >= limit:
                break
    return count


def has_replayable_page(settings: Settings, archive_path: str) -> bool:
    """Whether anything in this archive can be opened in the replay tab.

    Constant work in the common case — the first record of a healthy archive
    is usually the front page — rather than proportional to the archive.
    """
    return replayable_pages(settings, archive_path, limit=1) > 0


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


def write_templates(settings: Settings) -> Path | None:
    """Write the head insert that uncovers a curtained page, or remove it.

    Some sites answer 200 with the whole page and then draw a content warning
    over it — an iframe pointing at their gate, plus a stylesheet rule hiding
    everything else. Blogger does this, and measured on a real blog it is not
    something the operator can fix at capture time: the acceptance cookie and
    every client header were byte-identical across two runs ten hours apart,
    and the same 70 posts came back clean on the first and curtained on the
    second. `docs/06` has the numbers.

    So the page is complete in the WARC and the archive still cannot show it,
    and the only layer that can see the problem is the one rendering it.

    **This is the one place Cairn changes what a replayed page renders**, and
    it is bounded accordingly:

    - It removes nothing from the archive. The WARC is untouched, and so is
      every WACZ export — this is a template, not a rewrite of stored bytes.
    - It fires only on the same structural pair `interstitial.overlay_blocked`
      requires: a gate-framed iframe *and* a rule hiding the body. Either
      alone leaves the page as it was found.
    - It says so on the page, so nobody mistakes an altered rendering for the
      original, and it sets `data-cairn-overlay-removed` for anything reading
      the DOM.
    - `replay_uncover_overlays: false` turns it off, and then the file is
      removed rather than left behind to confuse the next person to read the
      directory.
    """
    from cairn.services import interstitial

    target = settings.replay_dir / TEMPLATES_DIR / HEAD_INSERT_FILE
    if not settings.replay_uncover_overlays:
        _remove(target)
        return None

    markers = json.dumps(list(interstitial.URL_MARKERS))
    body = _HEAD_INSERT_TEMPLATE.replace("__MARKERS__", markers)
    target.parent.mkdir(parents=True, exist_ok=True)
    storage.write_atomic(target, body)
    return target


# The markers are substituted from `interstitial.URL_MARKERS` rather than
# written out again: the check that reports a curtained capture and the script
# that uncovers it have to agree about what a gate looks like, and two hand-kept
# lists would not stay agreed.
_HEAD_INSERT_TEMPLATE = """\
{# GENERATED by cairn — do not hand-edit. #}
{# pywb's own head insert, included rather than copied. See replay.py. #}
{% include "head_insert.html" %}

<script>
/* Uncover a page the site drew a content warning over. Cairn; see docs/07. */
(function () {
  var MARKERS = __MARKERS__;
  var HIDES_BODY = /body\\s*\\*\\s*\\{[^}]{0,200}visibility\\s*:\\s*hidden/i;

  function gateFrame() {
    var frames = document.getElementsByTagName('iframe');
    for (var i = 0; i < frames.length; i++) {
      /* pywb has rewritten this src to point into the collection, but the
         gate's own path survives inside it, which is what we match. */
      var src = frames[i].getAttribute('src') || '';
      for (var j = 0; j < MARKERS.length; j++) {
        if (src.indexOf(MARKERS[j]) !== -1) { return frames[i]; }
      }
    }
    return null;
  }

  function hidingStyles() {
    var found = [];
    var styles = document.getElementsByTagName('style');
    for (var i = 0; i < styles.length; i++) {
      if (HIDES_BODY.test(styles[i].textContent || '')) { found.push(styles[i]); }
    }
    return found;
  }

  function note() {
    var el = document.createElement('div');
    el.setAttribute('data-cairn-note', '1');
    el.textContent = 'content warning not shown';
    el.title = 'This site drew a content warning over the page. The page was '
             + 'archived in full and Cairn is showing it. Nothing was removed '
             + 'from the archive itself.';
    el.style.cssText = 'position:fixed;left:8px;bottom:8px;z-index:2147483647;'
      + 'font:11px/1.7 system-ui,-apple-system,sans-serif;padding:1px 9px;'
      + 'background:rgba(24,24,27,.85);color:#fafafa;border-radius:11px;'
      + 'visibility:visible;opacity:.75;pointer-events:auto;';
    document.body.appendChild(el);
  }

  function uncover() {
    try {
      var frame = gateFrame();
      if (!frame) { return; }
      var styles = hidingStyles();
      /* A framed gate over a page that is still visible is a banner, not a
         curtain. Leave it: the reader can see the page and the warning both,
         which is the site's own design and none of our business. */
      if (!styles.length) { return; }
      frame.parentNode.removeChild(frame);
      for (var i = 0; i < styles.length; i++) {
        styles[i].parentNode.removeChild(styles[i]);
      }
      document.documentElement.setAttribute('data-cairn-overlay-removed', '1');
      note();
    } catch (e) {
      /* Never let this break a replayed page. A page that renders with the
         curtain is worse than one that renders without it; a page that does
         not render at all is worse than either. */
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', uncover);
  } else {
    uncover();
  }
})();
</script>
"""


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
    if settings.replay_uncover_overlays:
        # Points at our template, which includes pywb's. Omitted entirely when
        # the setting is off, so pywb falls back to its own default and the
        # replay path is exactly what it was before this existed.
        config["head_insert_html"] = HEAD_INSERT_FILE
    body = (
        "# GENERATED by cairn — do not hand-edit.\n"
        "# Collections are discovered from the collections/ tree, not listed here:\n"
        "# pywb picks up a collection created while it is running, so adding a\n"
        "# site needs no restart.\n" + yaml.safe_dump(config, sort_keys=True)
    )
    target = settings.replay_dir / CONFIG_FILE
    storage.write_atomic(target, body)
    return target
