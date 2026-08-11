"""M4's exit criterion, as a test.

*Twenty sites organized into a nested tree with tags, filterable in the UI, and
the same structure navigable on disk over SMB.*

SMB itself is not something a test can mount, so the two properties that make
a tree navigable over one are asserted directly instead: every folder is a
real directory at the path the API reports, and every `by-tag` entry is a
relative symlink that resolves to its site. Those are exactly the two things
that fail silently — an absolute link works perfectly on the container and
resolves to nothing on the client that mounted the share.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cairn.config import Settings
from tests.conftest import XHR

TREE = {
    "Blogs": ["Photography", "Cooking"],
    "Reference": ["Manuals"],
}
# Four tags against three leaf folders, on purpose. Equal cycles would make
# the folder and the tag of a site perfectly correlated, and every test of
# "folder AND tag" would pass without testing anything.
TAGS = ["travel", "food", "reference", "manuals"]
SITE_COUNT = 20


def folder_of(n: int) -> int:
    return n % 3


def tag_of(n: int) -> str:
    return TAGS[n % len(TAGS)]


@pytest.fixture
def organized(authed: TestClient) -> dict[str, int]:
    """Twenty sites across a nested tree, tagged in a repeating pattern."""
    folders: dict[str, int] = {}
    for parent_name, children in TREE.items():
        parent = authed.post("/api/folders", json={"name": parent_name}, headers=XHR)
        assert parent.status_code == 201, parent.text
        folders[parent_name] = parent.json()["id"]
        for child in children:
            created = authed.post(
                "/api/folders",
                json={"name": child, "parent_id": folders[parent_name]},
                headers=XHR,
            )
            assert created.status_code == 201, created.text
            folders[f"{parent_name}/{child}"] = created.json()["id"]

    leaves = [key for key in folders if "/" in key]
    assert len(leaves) == 3
    for n in range(SITE_COUNT):
        created = authed.post(
            "/api/sites",
            json={
                "seed_url": f"https://site-{n:02d}.example.com/",
                "title": f"Site {n:02d}",
                "folder_id": folders[leaves[folder_of(n)]],
                "tags": [tag_of(n)],
            },
            headers=XHR,
        )
        assert created.status_code == 201, created.text
    return folders


def test_twenty_sites_are_filterable_by_folder_and_tag(
    authed: TestClient, organized: dict[str, int]
) -> None:
    everything = authed.get("/api/sites?per_page=50", headers=XHR).json()
    assert everything["total"] == SITE_COUNT

    blogs = authed.get(f"/api/sites?folder_id={organized['Blogs']}", headers=XHR).json()
    photography = authed.get(
        f"/api/sites?folder_id={organized['Blogs/Photography']}", headers=XHR
    ).json()
    travel = authed.get("/api/sites?tag=travel&per_page=50", headers=XHR).json()

    # Blogs is the parent of two of the three leaf folders and holds nothing
    # directly, so its recursive count is the sum of its children's.
    assert blogs["total"] > photography["total"] > 0
    assert blogs["total"] == sum(
        authed.get(f"/api/sites?folder_id={organized[key]}", headers=XHR).json()["total"]
        for key in ("Blogs/Photography", "Blogs/Cooking")
    )
    assert travel["total"] == sum(1 for n in range(SITE_COUNT) if tag_of(n) == "travel")

    # The point of the compound query: strictly fewer than either side alone.
    combined = authed.get(
        f"/api/sites?folder_id={organized['Blogs']}&tag=travel&per_page=50", headers=XHR
    ).json()
    expected = sum(1 for n in range(SITE_COUNT) if tag_of(n) == "travel" and folder_of(n) in (0, 1))
    assert combined["total"] == expected
    assert 0 < combined["total"] < travel["total"]
    assert combined["total"] < blogs["total"]


def test_the_same_structure_is_on_disk(
    authed: TestClient, settings: Settings, organized: dict[str, int]
) -> None:
    """Every folder the API reports is a directory, and every site is inside
    the one the API says it is in."""
    for node in _flatten(authed.get("/api/folders", headers=XHR).json()):
        assert (settings.archives_dir / node["path"]).is_dir(), f"{node['path']} is not on disk"

    for site in authed.get("/api/sites?per_page=50", headers=XHR).json()["items"]:
        directory = settings.archives_dir / site["archive_path"]
        assert directory.is_dir(), f"{site['archive_path']} is not on disk"
        assert directory.parent == settings.archives_dir / site["folder_path"]
        assert (directory / "site.yaml").is_file()


@pytest.mark.skipif(os.name == "nt", reason="symlinks need elevation on Windows")
def test_every_tag_is_a_directory_of_relative_links(
    authed: TestClient, settings: Settings, organized: dict[str, int]
) -> None:
    authed.post("/api/maintenance/rebuild-symlinks", headers=XHR)

    for tag in TAGS:
        directory = settings.by_tag_dir / tag
        assert directory.is_dir(), f"by-tag/{tag} is missing"
        entries = list(directory.iterdir())
        assert entries, f"by-tag/{tag} is empty"
        for entry in entries:
            assert entry.is_symlink()
            target = os.readlink(entry)
            assert not os.path.isabs(target), (
                f"{entry} points at {target}, which resolves against the client's "
                "filesystem over SMB and is refused by Samba as a wide link"
            )
            assert (entry / "site.yaml").is_file(), f"{entry} does not resolve to a site"


def test_renaming_a_folder_carries_every_site_under_it(
    authed: TestClient, settings: Settings, organized: dict[str, int]
) -> None:
    """One rename on disk, however many rows it rewrites."""
    before = authed.get(
        f"/api/sites?folder_id={organized['Blogs']}&per_page=50", headers=XHR
    ).json()["items"]

    renamed = authed.patch(
        f"/api/folders/{organized['Blogs']}", json={"name": "Weblogs"}, headers=XHR
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json() == {
        "status": "done",
        "method": "rename",
        "path": "Weblogs",
        "job_id": None,
    }

    assert not (settings.archives_dir / "Blogs").exists()
    after = authed.get(
        f"/api/sites?folder_id={organized['Blogs']}&per_page=50", headers=XHR
    ).json()["items"]
    assert len(after) == len(before)
    for site in after:
        assert site["archive_path"].startswith("Weblogs/")
        assert (settings.archives_dir / site["archive_path"] / "site.yaml").is_file()


def test_the_storage_report_adds_up(
    authed: TestClient, settings: Settings, organized: dict[str, int]
) -> None:
    report = authed.get("/api/storage", headers=XHR).json()

    assert report["sites"] == SITE_COUNT
    assert report["trash_sites"] == 0
    paths = {folder["path"] for folder in report["folders"]}
    assert {"Blogs", "Blogs/Photography", "Reference/Manuals"} <= paths

    blogs = next(f for f in report["folders"] if f["path"] == "Blogs")
    assert blogs["site_count"] == 0
    assert blogs["total_site_count"] > 0


def test_deleting_a_site_and_restoring_it_keeps_it_in_its_folder(
    authed: TestClient, settings: Settings, organized: dict[str, int]
) -> None:
    site = authed.get("/api/sites?per_page=1", headers=XHR).json()["items"][0]

    authed.delete(f"/api/sites/{site['id']}", headers=XHR)
    listed = authed.get("/api/sites?per_page=50", headers=XHR).json()
    assert listed["total"] == SITE_COUNT - 1
    assert not (settings.archives_dir / site["archive_path"]).exists()

    trashed = authed.get("/api/trash", headers=XHR).json()
    assert [entry["id"] for entry in trashed] == [site["id"]]

    restored = authed.post(f"/api/sites/{site['id']}/restore", headers=XHR)
    assert restored.status_code == 200, restored.text
    assert restored.json()["archive_path"] == site["archive_path"]
    assert (settings.archives_dir / site["archive_path"]).is_dir()
    assert authed.get("/api/sites?per_page=50", headers=XHR).json()["total"] == SITE_COUNT


def _flatten(nodes: list[dict]) -> list[dict]:
    return [n for node in nodes for n in [node, *_flatten(node["children"])]]


def test_a_folder_directory_recreated_empty_is_restored_at_boot(
    authed: TestClient, settings: Settings, organized: dict[str, int], tmp_path: Path
) -> None:
    """The repair path for a volume that came back without its tree."""
    import shutil

    from cairn.db.bootstrap import reconcile_organization

    shutil.rmtree(settings.archives_dir / "Reference")
    factory = authed.app.state.sessionmaker  # type: ignore[attr-defined]
    with factory() as session:
        reconcile_organization(session, settings)
        session.commit()

    assert (settings.archives_dir / "Reference" / "Manuals").is_dir()
