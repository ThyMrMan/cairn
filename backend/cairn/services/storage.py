"""On-disk layout: site directories, capture directories, and the two files
that make the database rebuildable.

The database is an index over the filesystem, not the system of record
(docs/03). Every site directory carries a `site.yaml` and every capture a
`manifest.json`; between them a rebuild job can reconstruct sites, captures,
and scope without the database existing at all. That property only holds if
these files are written on every change, so they are written here rather than
opportunistically at the call sites.
"""

from __future__ import annotations

import errno
import json
import os
import re
import shutil
import tempfile
import unicodedata
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from cairn.config import Settings
from cairn.db.types import to_iso, utcnow

SITE_FILE = "site.yaml"
MANIFEST_FILE = "manifest.json"
CAPTURES_DIR = "captures"
INDEX_DIR = "index"
DERIVED_DIR = "derived"
EXPORTS_DIR = "exports"
WARC_DIR = "warc"

SCHEMA_VERSION = 1
MAX_SLUG_LENGTH = 64
SLUG_COLLISION_LIMIT = 1000

# Windows reserves these regardless of extension. Cairn targets Linux, but the
# archive tree is routinely read over SMB from Windows, and a site directory
# named "con" is one nobody can open from there.
_RESERVED_NAMES = frozenset(
    {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
     *(f"lpt{i}" for i in range(1, 10))}
)  # fmt: skip

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")
_DIR_NAME = re.compile(r"^\d{8}T\d{6}Z-[a-z0-9]+-[a-z0-9]+$")
# Illegal in a Windows filename, and therefore unusable over SMB even though
# Linux would take most of them. `:` is the worst of the set — it opens an
# alternate data stream rather than failing, so the file appears to vanish.
_DIR_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

MAX_DIR_NAME_LENGTH = 96


class StoragePathError(ValueError):
    """A path escaped the directory it was supposed to stay inside."""


class CrossDeviceMoveError(OSError):
    """A rename would have crossed a filesystem boundary.

    Raised rather than silently copying, because the two are not the same
    operation: a rename is instant whatever the directory holds, and a copy of
    a site's archive is minutes and a second copy of the bytes. The caller
    decides — in practice by moving the work into a job.
    """


# ── naming ───────────────────────────────────────────────────────────────


def slugify(value: str, *, fallback: str = "site") -> str:
    """Lowercase `[a-z0-9-]` only.

    Slugs are stable identity: renaming a site's title must never move its
    files (docs/03), so this is only ever called when a slug is first minted.
    """
    normalized = unicodedata.normalize("NFKD", value)
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii")
    slug = _SLUG_STRIP.sub("-", ascii_only.lower()).strip("-")
    slug = slug[:MAX_SLUG_LENGTH].strip("-")
    if not slug or slug in _RESERVED_NAMES:
        slug = f"{slug or fallback}-x" if slug else fallback
    return slug


def unique_slug(base: str, taken: set[str]) -> str:
    """Append `-2`, `-3`, … until the slug is free within its folder."""
    if base not in taken:
        return base
    for suffix in range(2, SLUG_COLLISION_LIMIT):
        candidate = f"{base}-{suffix}"
        if candidate not in taken:
            return candidate
    raise StoragePathError(f"could not find a free slug for {base!r}")


def dir_name(value: str, *, fallback: str = "") -> str:
    """A display name reduced to something usable as a directory component.

    Unlike `slugify`, this keeps capitals, spaces and non-ASCII letters: the
    archive tree is browsed over SMB, and `Blogs/Photography` is the point of
    having folders at all — `blogs/photography` would be a lesser version of
    the same thing, and `f3a9/` no version of it.

    What it removes is what a share cannot carry. Trailing dots and spaces are
    stripped because Windows silently drops them, so `Photos.` and `Photos`
    would be one directory reached by two names.
    """
    cleaned = _DIR_ILLEGAL.sub(" ", unicodedata.normalize("NFC", value or ""))
    cleaned = " ".join(cleaned.split()).rstrip(". ")
    if not cleaned or cleaned in {".", ".."}:
        return fallback
    if cleaned.split(".")[0].lower() in _RESERVED_NAMES:
        return fallback
    return cleaned[:MAX_DIR_NAME_LENGTH].rstrip(". ") or fallback


def engine_short_name(engine_id: str) -> str:
    """`wget-warc` → `wget`. Used in capture directory names."""
    return slugify(engine_id.split("-")[0], fallback="engine")


def capture_dir_name(started_at: datetime, kind: str, engine_id: str) -> str:
    """`20260809T142530Z-full-wget` — sorts chronologically, self-describing.

    Basic-format ISO 8601 rather than extended: colons are legal on Linux but
    make the path unusable over SMB and awkward in shell arguments.
    """
    aware = started_at if started_at.tzinfo else started_at.replace(tzinfo=UTC)
    stamp = aware.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{slugify(kind, fallback='run')}-{engine_short_name(engine_id)}"


def is_capture_dir_name(name: str) -> bool:
    """Whether a directory name is one we produced — used by the rebuild scan."""
    return bool(_DIR_NAME.match(name))


# ── path safety ──────────────────────────────────────────────────────────


def resolve_within(base: Path, relative: str | Path) -> Path:
    """Resolve `relative` under `base`, refusing anything that escapes.

    Engines report artifact paths and users name folders; both reach the
    filesystem. Symlinks are resolved before the check, so a symlink planted
    inside the capture directory cannot be used to record a file outside it
    (docs/05).
    """
    base_resolved = base.resolve()
    candidate = (base_resolved / relative).resolve()
    if candidate != base_resolved and not candidate.is_relative_to(base_resolved):
        raise StoragePathError(f"{relative!r} resolves outside {base_resolved}")
    return candidate


# ── atomic writes ────────────────────────────────────────────────────────


def write_atomic(path: Path, data: str | bytes, *, mode: int | None = None) -> None:
    """Write via a temp file in the same directory, then rename.

    A half-written `site.yaml` is worse than a missing one: the rebuild job
    would read it, believe it, and reconstruct a site with truncated scope.
    Same directory matters — `os.replace` is only atomic within a filesystem.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    binary = isinstance(data, bytes)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb" if binary else "w", encoding=None if binary else "utf-8") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        if mode is not None:
            tmp.chmod(mode)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def write_json(path: Path, payload: Any) -> None:
    write_atomic(path, json.dumps(payload, indent=2, sort_keys=False, default=str) + "\n")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_yaml(path: Path, payload: Any) -> None:
    write_atomic(path, yaml.safe_dump(payload, sort_keys=False, allow_unicode=True))


def read_yaml(path: Path) -> Any:
    # safe_load, always: site.yaml sits in a directory the user can write to
    # over SMB, and full_load there is arbitrary object construction.
    return yaml.safe_load(path.read_text(encoding="utf-8"))


# ── site directories ─────────────────────────────────────────────────────


def site_dir(settings: Settings, archive_path: str) -> Path:
    """Absolute path of a site directory from its stored relative path."""
    return resolve_within(settings.archives_dir, archive_path)


def ensure_site_dirs(settings: Settings, archive_path: str) -> Path:
    root = site_dir(settings, archive_path)
    for sub in (CAPTURES_DIR, INDEX_DIR, DERIVED_DIR, EXPORTS_DIR):
        (root / sub).mkdir(parents=True, exist_ok=True)
    return root


def ensure_capture_dirs(settings: Settings, archive_path: str, dir_name: str) -> Path:
    root = site_dir(settings, archive_path) / CAPTURES_DIR
    capture = resolve_within(root, dir_name)
    (capture / WARC_DIR).mkdir(parents=True, exist_ok=True)
    return capture


def site_yaml_path(settings: Settings, archive_path: str) -> Path:
    return site_dir(settings, archive_path) / SITE_FILE


def manifest_path(settings: Settings, archive_path: str, dir_name: str) -> Path:
    return site_dir(settings, archive_path) / CAPTURES_DIR / dir_name / MANIFEST_FILE


# ── moving directories ───────────────────────────────────────────────────

STAGING_PREFIX = ".moving-"


def rename_directory(source: Path, target: Path) -> None:
    """Move a directory by rename, or refuse and say why.

    Within one filesystem this is the whole operation, whatever is underneath —
    a 40 GB site moves as fast as an empty one, which is why folder moves do
    not need to care how much they contain.

    Raises `CrossDeviceMoveError` when the two ends are on different filesystems.
    That happens on Unraid, where `/data` can span array disks behind a FUSE
    layer, and the honest answer there is a copy, which is a different
    operation with a different cost.
    """
    if not source.exists():
        raise StoragePathError(f"{source} does not exist")
    if target.exists():
        raise StoragePathError(f"{target} already exists")
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.rename(source, target)
    except OSError as exc:
        if exc.errno == errno.EXDEV:
            raise CrossDeviceMoveError(exc.errno, str(exc), str(source)) from exc
        raise


def copy_directory_into_place(source: Path, target: Path) -> None:
    """The cross-device fallback, staged so a crash cannot lose the archive.

    Copy into a sibling of the target, rename that into place, and only then
    delete the source. `shutil.move` does copytree-then-rmtree directly onto
    the target, so a container stopped mid-copy — which on Unraid it will be —
    leaves a half-written directory that looks like a real site and a source
    that may already be partly deleted.

    Staged, the same crash leaves an intact source and a `.moving-` directory
    that the boot sweep can recognise and remove. The rename at the end is
    within one directory, so it is atomic.
    """
    if not source.exists():
        raise StoragePathError(f"{source} does not exist")
    if target.exists():
        raise StoragePathError(f"{target} already exists")
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.parent / f"{STAGING_PREFIX}{target.name}"
    if staging.exists():
        shutil.rmtree(staging)
    try:
        shutil.copytree(source, staging, symlinks=True)
        os.replace(staging, target)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    shutil.rmtree(source, ignore_errors=True)


def sweep_staging(root: Path) -> int:
    """Remove `.moving-` leftovers from a move that died mid-copy."""
    removed = 0
    if not root.exists():
        return 0
    for path in root.rglob(f"{STAGING_PREFIX}*"):
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
    return removed


# ── the disk-side records ────────────────────────────────────────────────


def build_site_yaml(
    *,
    site_id: int,
    slug: str,
    title: str,
    seed_url: str,
    primary_host: str,
    folder: str,
    tags: list[str],
    engine_id: str,
    access_profile: str | None,
    scope: dict[str, Any],
    feeds: list[dict[str, Any]],
    created_at: datetime,
) -> dict[str, Any]:
    """The docs/03 site.yaml shape.

    `access_profile` is the profile *name* and never its material — this file
    lives in the archive tree, which is shared over SMB and copied to backups.
    """
    return {
        "schema": SCHEMA_VERSION,
        "id": site_id,
        "slug": slug,
        "title": title,
        "seed_url": seed_url,
        "primary_host": primary_host,
        "folder": folder,
        "tags": tags,
        "engine": engine_id,
        "access_profile": access_profile,
        "scope": scope,
        "feeds": feeds,
        "created_at": to_iso(created_at),
        "updated_at": to_iso(utcnow()),
    }


def build_manifest(
    *,
    capture_id: int,
    site_slug: str,
    kind: str,
    engine_id: str,
    engine_version: str,
    tool_version: str | None,
    started_at: datetime,
    finished_at: datetime | None,
    status: str,
    seeds: list[str],
    seed_source: dict[str, int],
    scope: dict[str, Any],
    stats: dict[str, Any],
    warc_files: list[dict[str, Any]],
) -> dict[str, Any]:
    """The docs/03 manifest.json shape."""
    return {
        "schema": SCHEMA_VERSION,
        "capture_id": capture_id,
        "site_slug": site_slug,
        "kind": kind,
        "engine": {"id": engine_id, "version": engine_version, "tool_version": tool_version},
        "started_at": to_iso(started_at),
        "finished_at": to_iso(finished_at) if finished_at else None,
        "status": status,
        "seeds": seeds,
        "seed_source": seed_source,
        "scope": scope,
        "stats": stats,
        "warc_files": warc_files,
    }


# ── measurement ──────────────────────────────────────────────────────────


def directory_size(path: Path) -> int:
    """Bytes on disk beneath `path`, following no symlinks.

    Symlinks are skipped rather than followed: /data/by-tag is a generated
    symlink tree into the archives, and following them would count every
    tagged site once per tag (docs/03).
    """
    total = 0
    if not path.exists():
        return 0
    for root, dirs, files in os.walk(path, followlinks=False):
        dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(root, d))]
        for name in files:
            entry = os.path.join(root, name)
            if os.path.islink(entry):
                continue
            try:
                total += os.stat(entry).st_size
            except OSError:  # pragma: no cover — races with a concurrent purge
                continue
    return total
