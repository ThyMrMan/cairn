"""Where the space went.

Every number here comes from `sites.size_bytes`, which the `stats`
post-processor measures by walking the site directory after each capture — not
from walking the tree on request. On a NAS array that walk is thousands of
cold `stat` calls and the page would take seconds to load while spinning up
disks nobody asked to wake.

The consequence is worth being honest about in the UI: these totals are as of
each site's last capture. Free space and trash are measured live, because
those are one `statvfs` and one directory that is normally small.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cairn.config import Settings
from cairn.db.models import Site
from cairn.services import folders, trash


@dataclass(slots=True)
class FolderUsage:
    id: int
    path: str
    site_count: int
    size_bytes: int
    total_site_count: int
    total_size_bytes: int


@dataclass(slots=True)
class Report:
    data_dir: str
    total_bytes: int
    used_bytes: int
    free_bytes: int
    sites: int
    archives_bytes: int
    trash_sites: int
    trash_bytes: int
    folders: list[FolderUsage] = field(default_factory=list)
    largest_sites: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "data_dir": self.data_dir,
            "total_bytes": self.total_bytes,
            "used_bytes": self.used_bytes,
            "free_bytes": self.free_bytes,
            "sites": self.sites,
            "archives_bytes": self.archives_bytes,
            "trash_sites": self.trash_sites,
            "trash_bytes": self.trash_bytes,
            "folders": [
                {
                    "id": f.id,
                    "path": f.path,
                    "site_count": f.site_count,
                    "size_bytes": f.size_bytes,
                    "total_site_count": f.total_site_count,
                    "total_size_bytes": f.total_size_bytes,
                }
                for f in self.folders
            ],
            "largest_sites": self.largest_sites,
        }


LARGEST_SITES = 10


def report(session: Session, settings: Settings) -> Report:
    usage = shutil.disk_usage(settings.data_dir)
    live = select(Site).where(Site.deleted_at.is_(None))

    site_count = session.scalar(select(func.count()).select_from(live.subquery())) or 0
    archives_bytes = (
        session.scalar(
            select(func.coalesce(func.sum(Site.size_bytes), 0)).where(Site.deleted_at.is_(None))
        )
        or 0
    )
    trashed = session.scalar(select(func.count(Site.id)).where(Site.deleted_at.isnot(None))) or 0

    flat: list[FolderUsage] = []

    def walk(nodes: list[folders.FolderNode]) -> None:
        for node in nodes:
            flat.append(
                FolderUsage(
                    id=node.folder.id,
                    path=node.folder.path,
                    site_count=node.site_count,
                    size_bytes=node.size_bytes,
                    total_site_count=node.total_site_count,
                    total_size_bytes=node.total_size_bytes,
                )
            )
            walk(node.children)

    walk(folders.tree(session))

    biggest = session.scalars(live.order_by(Site.size_bytes.desc()).limit(LARGEST_SITES)).all()

    return Report(
        data_dir=str(settings.data_dir),
        total_bytes=usage.total,
        used_bytes=usage.used,
        free_bytes=usage.free,
        sites=int(site_count),
        archives_bytes=int(archives_bytes),
        trash_sites=int(trashed),
        trash_bytes=trash.trash_size(settings),
        folders=flat,
        largest_sites=[
            {
                "id": s.id,
                "title": s.title,
                "path": s.archive_path,
                "size_bytes": s.size_bytes,
                "url_count": s.url_count,
            }
            for s in biggest
            if s.size_bytes
        ],
    )
