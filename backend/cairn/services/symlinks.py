r"""`/data/by-tag` — the tag structure, on disk.

Folders are already real directories, so browsing the folder tree over SMB
needs nothing built. Tags are the half with no natural place on a filesystem:
a site has several, and a directory has one path. Symlinks are how a site
appears under each of its tags without the bytes existing twice.

**The links are relative, and that is not a style preference.** The tree is
read over SMB, where `/data` is not the root of anything — the share is
mounted as `Z:\` or `/mnt/tower/cairn`, and an absolute `/data/archives/…`
link resolves against the *client's* filesystem, where it means nothing. Samba
compounds it: `wide links` defaults to off, so a link whose target appears to
leave the share is refused outright. A relative `../../archives/Blogs/example`
stays inside the share and is followed on both counts.

**The tree is always rebuilt whole, never nudged.** docs/03 imagined a
debounced incremental refresh; at any scale this tool reaches, rebuilding is a
few hundred `symlink(2)` calls and finishes faster than the request that
triggered it. Incremental was strictly worse: identical cost, plus the one
failure mode that matters here — a tree that quietly stops matching the
database and looks fine until somebody trusts it.

Everything here is derived and disposable. Failing to link never fails the
operation that triggered it: on a Windows development machine symlinks need
elevation and simply do not happen, and that must cost the tag tree rather
than the tag.
"""

from __future__ import annotations

import os
import shutil
from collections import defaultdict
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from cairn.config import Settings
from cairn.db.models import Site, SiteTag, Tag
from cairn.logging import get_logger
from cairn.services import storage

log = get_logger(__name__)


class SymlinkError(RuntimeError):
    """The tag tree could not be written. Never fatal to the caller."""


# Whether this process has already reported that symlinks are unavailable.
# A filesystem either takes them or it does not, so the second failure carries
# no information the first did not — and the tree is rebuilt on every tag
# change, so without this a Windows development machine emits a warning per
# site per tag and buries every other log line under it.
_reported = False


def _warn_once(failures: list[str], linked: int) -> None:
    global _reported
    if _reported:
        return
    _reported = True
    log.warning(
        "could not write the tag tree, so /data/by-tag will be incomplete. Tags "
        "themselves are unaffected. This is expected on a filesystem that does not "
        "allow symlinks — Windows needs Developer Mode or elevation.",
        extra={"failed": len(failures), "linked": linked, "first": failures[0]},
    )


def tag_dir(settings: Settings, tag_slug: str) -> Path:
    return settings.by_tag_dir / tag_slug


def plan(session: Session) -> dict[str, dict[str, str]]:
    """`{tag slug: {link name: site archive_path}}` — the tree as it should be.

    Site slugs are unique within a folder, not globally, so two sites in
    different folders can both be `example`. When that happens *inside one
    tag*, both get their id appended — both, not just the newcomer, so the
    answer depends only on the data and not on the order rows arrived in. A
    name that depends on insertion order cannot be recomputed, which is the
    same as saying the tree could never be checked.
    """
    rows = session.execute(
        select(Tag.slug, Site.slug, Site.archive_path, Site.id)
        .join(SiteTag, SiteTag.tag_id == Tag.id)
        .join(Site, Site.id == SiteTag.site_id)
        .where(Site.deleted_at.is_(None))
        .order_by(Tag.slug, Site.id)
    ).all()

    by_tag: dict[str, list[tuple[str, str, int]]] = defaultdict(list)
    for tag_slug, site_slug, archive_path, site_id in rows:
        by_tag[tag_slug].append((site_slug, archive_path, site_id))

    tree: dict[str, dict[str, str]] = {}
    for tag_slug, sites in by_tag.items():
        seen: dict[str, int] = defaultdict(int)
        for site_slug, _, _ in sites:
            seen[site_slug] += 1
        tree[tag_slug] = {
            (site_slug if seen[site_slug] == 1 else f"{site_slug}-{site_id}"): archive_path
            for site_slug, archive_path, site_id in sites
        }
    return tree


def rebuild(session: Session, settings: Settings) -> tuple[int, int]:
    """Make `/data/by-tag` match the database. Returns (links, removals)."""
    wanted = plan(session)
    settings.by_tag_dir.mkdir(parents=True, exist_ok=True)

    linked = 0
    failures: list[str] = []
    for tag_slug, entries in wanted.items():
        directory = tag_dir(settings, tag_slug)
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:  # pragma: no cover — permissions
            failures.append(str(exc))
            continue
        for name, archive_path in entries.items():
            try:
                _link(directory / name, storage.site_dir(settings, archive_path))
                linked += 1
            except (SymlinkError, storage.StoragePathError) as exc:
                failures.append(str(exc))

    if failures:
        _warn_once(failures, linked)

    removed = _prune(settings, wanted)
    return linked, removed


def _prune(settings: Settings, wanted: dict[str, dict[str, str]]) -> int:
    """Drop links and directories the database no longer asks for.

    Only ever removes symlinks and the empty directories that held them. A
    real directory under `by-tag` is somebody's data — put there by hand over
    the share, most likely — and deleting it because it is not in the database
    would be this tool losing something it never owned.
    """
    removed = 0
    for directory in _tag_dirs(settings):
        keep = wanted.get(directory.name, {})
        for entry in _entries(directory):
            if entry.is_symlink() and entry.name not in keep:
                entry.unlink(missing_ok=True)
                removed += 1
        if not keep and not _entries(directory):
            shutil.rmtree(directory, ignore_errors=True)
    return removed


def _link(link: Path, target: Path) -> None:
    wanted = os.path.relpath(target, link.parent)
    if link.is_symlink():
        if os.readlink(link) == wanted:
            return
        link.unlink()
    elif link.exists():
        raise SymlinkError(f"{link} exists and is not a symlink")
    try:
        os.symlink(wanted, link, target_is_directory=True)
    except OSError as exc:
        raise SymlinkError(f"could not link {link} -> {wanted}: {exc}") from exc


def _tag_dirs(settings: Settings) -> list[Path]:
    if not settings.by_tag_dir.is_dir():
        return []
    return [p for p in settings.by_tag_dir.iterdir() if p.is_dir() and not p.is_symlink()]


def _entries(directory: Path) -> list[Path]:
    try:
        return list(directory.iterdir())
    except OSError:  # pragma: no cover — races with a concurrent rebuild
        return []


def safe_rebuild(session: Session, settings: Settings) -> None:
    """`rebuild`, with any failure downgraded to a log line.

    What every caller in the request path wants: the tag was set either way,
    and a filesystem that will not take symlinks is not a reason to fail the
    request that set it.
    """
    try:
        rebuild(session, settings)
    except OSError as exc:
        log.warning("could not rebuild the tag tree", extra={"err": str(exc)})
