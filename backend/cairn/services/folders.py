"""Folders: the tree in the UI and the directory tree on disk, kept identical.

That identity is the milestone, not a convenience. "The same structure is
navigable on disk over SMB" is M4's exit criterion, so a folder is a real
directory under `/data/archives` from the moment it is created — an empty
folder that exists only as a database row would look right in the UI and be
missing from the share.

Three columns carry the tree, and they do different jobs:

  - **`name`** is what a person typed. Shown in the UI, and — sanitized — used
    as the directory component, because a share full of `f3a9/` tells nobody
    anything.
  - **`slug`** is the uniqueness key within a parent. Case-folded and
    punctuation-stripped, so `Blogs` and `blogs ` cannot become two folders
    that are one directory on a case-insensitive filesystem. It is stricter
    than the filesystem needs: `Foo Bar` and `Foo-Bar` both slug to `foo-bar`
    and the second is refused, even though they are distinct directories.
    Over-strict in the safe direction is the right trade here — the failure it
    prevents is two folders silently sharing one directory over SMB.
  - **`path`** is the materialized path, `Blogs/Photography`. Read constantly,
    rewritten rarely.

Renaming or reparenting rewrites `path` for the folder, every descendant, and
every site beneath it — but touches the disk exactly once, because renaming a
directory carries everything under it. The database work is the expensive half.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cairn.config import Settings
from cairn.db.models import Folder, Site
from cairn.db.types import utcnow
from cairn.logging import get_logger
from cairn.services import storage

log = get_logger(__name__)

# The `path` column is 1024 characters and a deep tree is a UI problem long
# before it is a storage one. Refuse rather than truncate.
MAX_DEPTH = 12
MAX_FOLDERS = 5_000


class FolderError(ValueError):
    """A folder could not be created or changed as requested."""


# ── reads ────────────────────────────────────────────────────────────────


def get_folder(session: Session, folder_id: int) -> Folder | None:
    return session.get(Folder, folder_id)


def require_folder(session: Session, folder_id: int) -> Folder:
    folder = session.get(Folder, folder_id)
    if folder is None:
        raise FolderError(f"folder {folder_id} does not exist")
    return folder


def root_folder(session: Session) -> Folder:
    """The default folder every site lands in when none is chosen."""
    folder = session.scalars(
        select(Folder).where(Folder.parent_id.is_(None)).order_by(Folder.id).limit(1)
    ).first()
    if folder is None:  # pragma: no cover — seed_defaults always creates one
        raise FolderError("no folders exist; the default folder is missing")
    return folder


def descendants(session: Session, folder: Folder) -> list[Folder]:
    """Every folder beneath this one, shallowest first.

    `startswith(..., autoescape=True)` matters: a folder may legitimately be
    named `50% off`, and an unescaped `%` in a LIKE pattern turns the prefix
    match into a wildcard that sweeps up unrelated branches of the tree.
    """
    return list(
        session.scalars(
            select(Folder)
            .where(Folder.path.startswith(f"{folder.path}/", autoescape=True))
            .order_by(Folder.path)
        ).all()
    )


def subtree_ids(session: Session, folder: Folder) -> list[int]:
    return [folder.id, *(f.id for f in descendants(session, folder))]


@dataclass(slots=True)
class FolderNode:
    folder: Folder
    site_count: int = 0
    total_site_count: int = 0
    size_bytes: int = 0
    total_size_bytes: int = 0
    children: list[FolderNode] = field(default_factory=list)


def tree(session: Session) -> list[FolderNode]:
    """The whole tree with counts and sizes rolled up into every ancestor.

    Built in one pass over two queries rather than recursing per node: a
    per-node COUNT is what makes a folder sidebar slow, and it degrades
    exactly when someone has enough folders to want one.
    """
    folders = list(session.scalars(select(Folder).order_by(Folder.sort_order, Folder.path)).all())
    rows = session.execute(
        select(Site.folder_id, func.count(Site.id), func.coalesce(func.sum(Site.size_bytes), 0))
        .where(Site.deleted_at.is_(None))
        .group_by(Site.folder_id)
    ).all()
    counts = {int(fid): (int(n), int(size)) for fid, n, size in rows}

    nodes = {
        f.id: FolderNode(
            folder=f,
            site_count=counts.get(f.id, (0, 0))[0],
            size_bytes=counts.get(f.id, (0, 0))[1],
        )
        for f in folders
    }

    roots: list[FolderNode] = []
    for folder in folders:
        node = nodes[folder.id]
        parent = nodes.get(folder.parent_id) if folder.parent_id is not None else None
        if parent is None:
            roots.append(node)
        else:
            parent.children.append(node)

    # Deepest first, so a child's totals are final before its parent adds them.
    for folder in sorted(folders, key=lambda f: f.path.count("/"), reverse=True):
        node = nodes[folder.id]
        node.total_site_count += node.site_count
        node.total_size_bytes += node.size_bytes
        parent = nodes.get(folder.parent_id) if folder.parent_id is not None else None
        if parent is not None:
            parent.total_site_count += node.total_site_count
            parent.total_size_bytes += node.total_size_bytes

    return roots


# ── writes ───────────────────────────────────────────────────────────────


def create_folder(
    session: Session, settings: Settings, *, name: str, parent_id: int | None = None
) -> Folder:
    parent = require_folder(session, parent_id) if parent_id is not None else None
    if parent is not None and _depth(parent) + 1 >= MAX_DEPTH:
        raise FolderError(f"folders can nest {MAX_DEPTH} deep at most")
    if (session.scalar(select(func.count(Folder.id))) or 0) >= MAX_FOLDERS:
        raise FolderError(f"there are already {MAX_FOLDERS} folders")

    display, slug = _validate_name(name)
    _reject_sibling_collision(session, parent_id=parent_id, slug=slug, exclude_id=None)

    path = f"{parent.path}/{display}" if parent is not None else display
    if session.scalar(select(Folder.id).where(Folder.path == path)):
        raise FolderError(f"a folder already exists at {path!r}")

    folder = Folder(
        parent_id=parent.id if parent is not None else None,
        name=display,
        slug=slug,
        path=path,
        sort_order=_next_sort_order(session, parent_id),
        created_at=utcnow(),
    )
    session.add(folder)
    session.flush()

    # On disk immediately, so a folder made in the UI is a folder on the share
    # before anything is put in it.
    ensure_dir(settings, folder)
    log.info("folder created", extra={"folder": folder.id, "path": folder.path})
    return folder


def ensure_dir(settings: Settings, folder: Folder) -> Path:
    directory = storage.resolve_within(settings.archives_dir, folder.path)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


@dataclass(slots=True)
class Relocation:
    """A pending path change: one directory move, many row rewrites.

    Computed before anything is touched so the caller can move the directory
    first and only then commit the rows — a rename that fails must leave the
    database describing where the files still are.
    """

    folder: Folder
    old_path: str
    new_path: str
    source: Path
    target: Path
    folders: list[tuple[Folder, str]] = field(default_factory=list)
    sites: list[tuple[Site, str]] = field(default_factory=list)

    @property
    def is_noop(self) -> bool:
        return self.old_path == self.new_path


def plan_rename(session: Session, settings: Settings, folder: Folder, *, name: str) -> Relocation:
    display, slug = _validate_name(name)
    _reject_sibling_collision(session, parent_id=folder.parent_id, slug=slug, exclude_id=folder.id)
    parent = session.get(Folder, folder.parent_id) if folder.parent_id is not None else None
    new_path = f"{parent.path}/{display}" if parent is not None else display
    plan = _plan(session, settings, folder, new_path)
    folder.name = display
    folder.slug = slug
    return plan


def plan_reparent(
    session: Session, settings: Settings, folder: Folder, *, parent_id: int | None
) -> Relocation:
    if parent_id == folder.id:
        raise FolderError("a folder cannot contain itself")
    parent = require_folder(session, parent_id) if parent_id is not None else None

    if parent is not None:
        # The cycle check is a path prefix test rather than a walk up the
        # parent chain: the chain is what would be corrupted by getting this
        # wrong, so testing it against itself proves nothing.
        if parent.path == folder.path or parent.path.startswith(f"{folder.path}/"):
            raise FolderError("a folder cannot be moved inside one of its own descendants")
        deepest = max((_depth(f) for f in descendants(session, folder)), default=_depth(folder))
        if _depth(parent) + 1 + (deepest - _depth(folder)) >= MAX_DEPTH:
            raise FolderError(f"that move would nest folders more than {MAX_DEPTH} deep")

    _reject_sibling_collision(session, parent_id=parent_id, slug=folder.slug, exclude_id=folder.id)
    new_path = f"{parent.path}/{folder.name}" if parent is not None else folder.name
    plan = _plan(session, settings, folder, new_path)
    folder.parent_id = parent.id if parent is not None else None
    return plan


def _plan(session: Session, settings: Settings, folder: Folder, new_path: str) -> Relocation:
    if session.scalar(select(Folder.id).where(Folder.path == new_path, Folder.id != folder.id)):
        raise FolderError(f"a folder already exists at {new_path!r}")

    old_path = folder.path
    plan = Relocation(
        folder=folder,
        old_path=old_path,
        new_path=new_path,
        source=storage.resolve_within(settings.archives_dir, old_path),
        target=storage.resolve_within(settings.archives_dir, new_path),
    )
    if plan.is_noop:
        return plan

    plan.folders.append((folder, new_path))
    for child in descendants(session, folder):
        plan.folders.append((child, new_path + child.path[len(old_path) :]))

    # A site's directory is `<its folder's path>/<its slug>`, so the new path
    # falls out of the folder's new path. Trashed sites are included: their
    # directory is in `trash/` rather than here, but `archive_path` is where a
    # restore will put it back, and leaving it stale would restore into a
    # folder that no longer exists.
    new_by_folder = {f.id: path for f, path in plan.folders}
    for site in session.scalars(
        select(Site).where(Site.folder_id.in_(list(new_by_folder))).order_by(Site.id)
    ).all():
        plan.sites.append((site, f"{new_by_folder[site.folder_id]}/{site.slug}"))
    return plan


def apply(plan: Relocation) -> None:
    """Write the planned paths onto the rows. Call *after* the disk move."""
    for folder, path in plan.folders:
        folder.path = path
    for site, path in plan.sites:
        site.archive_path = path
        site.updated_at = utcnow()


def check_deletable(
    session: Session, folder: Folder, *, reassign_to: int | None
) -> tuple[list[Site], Folder | None]:
    """Decide whether a folder may go, and where its sites would land.

    `ON DELETE RESTRICT` on both foreign keys is deliberate (docs/03): a
    cascade here would delete site rows and leave the archives they point at
    as orphaned directories nobody can reach from the UI. So sites move
    somewhere explicitly, or nothing happens.

    Child folders are refused outright rather than swept along. Deleting one
    folder and silently relocating a whole subtree's worth of archives is not
    something a confirmation dialog can honestly describe.
    """
    if folder.parent_id is None and (
        session.scalar(select(func.count(Folder.id)).where(Folder.parent_id.is_(None))) == 1
    ):
        raise FolderError("the last top-level folder cannot be deleted; sites need somewhere to go")

    children = descendants(session, folder)
    if children:
        raise FolderError(
            f"that folder still holds {len(children)} folder(s) — delete or move those first"
        )

    # Trashed sites count. They still reference the folder, so leaving them out
    # would produce a delete that fails at the database with no visible cause.
    sites = list(session.scalars(select(Site).where(Site.folder_id == folder.id)).all())
    if not sites:
        return [], None

    if reassign_to is None:
        live = sum(1 for s in sites if s.deleted_at is None)
        trashed = len(sites) - live
        detail = f"{live} site(s)" if not trashed else f"{live} site(s) and {trashed} in the trash"
        raise FolderError(f"that folder still holds {detail}; choose where they should go first")

    target = require_folder(session, reassign_to)
    if target.id == folder.id:
        raise FolderError("sites cannot be reassigned to the folder being deleted")
    return sites, target


def _next_sort_order(session: Session, parent_id: int | None) -> int:
    highest = session.scalar(
        select(func.max(Folder.sort_order)).where(
            Folder.parent_id.is_(None) if parent_id is None else Folder.parent_id == parent_id
        )
    )
    return int(highest or 0) + 1


def _depth(folder: Folder) -> int:
    return folder.path.count("/")


def _validate_name(name: str) -> tuple[str, str]:
    display = storage.dir_name(name)
    if not display:
        raise FolderError(f"{name!r} is not a usable folder name")
    slug = storage.slugify(display, fallback="")
    if not slug:
        raise FolderError(f"{name!r} has no letters or digits in it")
    return display, slug


def _reject_sibling_collision(
    session: Session, *, parent_id: int | None, slug: str, exclude_id: int | None
) -> None:
    stmt = select(Folder.id, Folder.name).where(
        Folder.slug == slug,
        Folder.parent_id.is_(None) if parent_id is None else Folder.parent_id == parent_id,
    )
    if exclude_id is not None:
        stmt = stmt.where(Folder.id != exclude_id)
    row = session.execute(stmt.limit(1)).first()
    if row is not None:
        raise FolderError(
            f"{row[1]!r} is already here — folder names have to differ by more than "
            "punctuation or capitalisation, because on a share they would be one directory"
        )
