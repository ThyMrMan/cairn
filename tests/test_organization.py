"""Folders, tags, moves and the trash.

The thing under test throughout is that the database and the filesystem agree.
Almost every assertion here checks both — a folder rename that updates rows
and not directories, or directories and not rows, is the failure this whole
milestone exists to avoid, and either half alone looks fine.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from cairn.config import Settings
from cairn.db.models import Folder, Site
from cairn.services import folders, moves, storage, symlinks, tags, trash
from cairn.services import sites as site_service
from cairn.services.storage import CrossDeviceMoveError
from tests.conftest import XHR


@pytest.fixture
def seeded(db: Session) -> Folder:
    """The default folder, created by the app's own startup sequence."""
    return folders.root_folder(db)


def make_site(db: Session, settings: Settings, folder: Folder, host: str) -> Site:
    return site_service.create_site(db, settings, seed_url=f"https://{host}/", folder_id=folder.id)


# ── names on disk ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Photography", "Photography"),
        ("  Travel  Notes ", "Travel Notes"),
        ("Blogs/2019", "Blogs 2019"),
        ("what?", "what"),
        ("Photos.", "Photos"),
        ("con", ""),
        ("...", ""),
        ("", ""),
    ],
)
def test_folder_names_are_reduced_to_something_a_share_can_carry(raw: str, expected: str) -> None:
    """Capitals and spaces survive; what SMB cannot carry does not.

    `Photos.` is the one worth keeping an eye on — Windows silently drops a
    trailing dot, so it and `Photos` would be one directory under two names.
    """
    assert storage.dir_name(raw) == expected


def test_a_folder_exists_on_disk_the_moment_it_is_created(
    db: Session, settings: Settings, seeded: Folder
) -> None:
    folder = folders.create_folder(db, settings, name="Photography", parent_id=seeded.id)

    assert folder.path == f"{seeded.path}/Photography"
    assert (settings.archives_dir / seeded.path / "Photography").is_dir(), (
        "an empty folder that exists only as a row is missing from the share"
    )


def test_sibling_names_that_differ_only_by_punctuation_are_refused(
    db: Session, settings: Settings, seeded: Folder
) -> None:
    folders.create_folder(db, settings, name="Photo Blogs", parent_id=seeded.id)

    with pytest.raises(folders.FolderError, match="already here"):
        folders.create_folder(db, settings, name="photo-blogs", parent_id=seeded.id)


# ── moving ───────────────────────────────────────────────────────────────


def test_renaming_a_folder_moves_one_directory_and_rewrites_every_path_below(
    db: Session, settings: Settings, seeded: Folder
) -> None:
    blogs = folders.create_folder(db, settings, name="Blogs", parent_id=None)
    photo = folders.create_folder(db, settings, name="Photography", parent_id=blogs.id)
    site = make_site(db, settings, photo, "example.com")
    old_dir = storage.site_dir(settings, site.archive_path)
    assert old_dir.is_dir()

    plan = folders.plan_rename(db, settings, blogs, name="Weblogs")
    moves.relocate(db, settings, plan)

    assert blogs.path == "Weblogs"
    assert photo.path == "Weblogs/Photography"
    assert site.archive_path == "Weblogs/Photography/example-com"
    assert not old_dir.exists()
    assert storage.site_dir(settings, site.archive_path).is_dir()


def test_a_folder_cannot_be_moved_inside_its_own_descendant(
    db: Session, settings: Settings, seeded: Folder
) -> None:
    blogs = folders.create_folder(db, settings, name="Blogs", parent_id=None)
    photo = folders.create_folder(db, settings, name="Photography", parent_id=blogs.id)

    with pytest.raises(folders.FolderError, match="own descendants"):
        folders.plan_reparent(db, settings, blogs, parent_id=photo.id)


def test_moving_a_site_re_slugs_on_collision(
    db: Session, settings: Settings, seeded: Folder
) -> None:
    """A slug is stable identity everywhere except here.

    `UNIQUE(folder_id, slug)` has to hold, and a title rename must never move
    files — so this is the only operation allowed to change one.
    """
    other = folders.create_folder(db, settings, name="Archive", parent_id=None)
    first = make_site(db, settings, other, "example.com")
    second = make_site(db, settings, seeded, "example.com")
    assert first.slug == second.slug == "example-com"

    moves.move_site(db, settings, second, other)

    assert second.slug == "example-com-2"
    assert second.archive_path == "Archive/example-com-2"
    assert storage.site_dir(settings, second.archive_path).is_dir()
    assert storage.site_dir(settings, first.archive_path).is_dir()


def test_a_cross_device_rename_is_reported_rather_than_silently_copied(
    db: Session, settings: Settings, seeded: Folder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The distinction the whole move design rests on.

    A rename is instant; a copy is minutes and a second copy of the bytes. So
    `rename_directory` refuses to decide, and the caller turns the copy into a
    job. Simulated, because provoking a real EXDEV needs two filesystems.
    """
    target = folders.create_folder(db, settings, name="Archive", parent_id=None)
    site = make_site(db, settings, seeded, "example.com")
    original = site.archive_path

    def exdev(*_args: object, **_kwargs: object) -> None:
        raise OSError(18, "Invalid cross-device link")

    monkeypatch.setattr(os, "rename", exdev)

    with pytest.raises(CrossDeviceMoveError):
        moves.move_site(db, settings, site, target)

    assert site.archive_path == original, "the row moved even though the files did not"
    assert storage.site_dir(settings, original).is_dir()


def test_the_copy_fallback_leaves_the_source_intact_until_the_target_is_complete(
    tmp_path: Path,
) -> None:
    """Staged copy, so a container killed mid-move loses nothing.

    `shutil.move` copies straight onto the target and then deletes the source;
    interrupted, that leaves a half-written directory that looks like a real
    archive. Here the half-written state is a `.moving-` directory instead,
    and the source is untouched until the rename into place has happened.
    """
    source = tmp_path / "site"
    (source / "captures").mkdir(parents=True)
    (source / "captures" / "part.warc.gz").write_bytes(b"payload")
    target = tmp_path / "moved" / "site"

    storage.copy_directory_into_place(source, target)

    assert (target / "captures" / "part.warc.gz").read_bytes() == b"payload"
    assert not source.exists()
    assert not list(target.parent.glob(f"{storage.STAGING_PREFIX}*"))


def test_interrupted_staging_directories_are_swept(tmp_path: Path) -> None:
    stale = tmp_path / "Blogs" / f"{storage.STAGING_PREFIX}example"
    stale.mkdir(parents=True)
    (stale / "half.warc.gz").write_bytes(b"")

    assert storage.sweep_staging(tmp_path) == 1
    assert not stale.exists()


def test_a_site_cannot_be_moved_while_a_job_is_running(
    db: Session, settings: Settings, seeded: Folder
) -> None:
    """wget resolves the output directory once, when the job starts.

    Move the directory underneath it and nothing fails — the WARC just lands
    in an inode the database has no name for.
    """
    from cairn.db.models import Job

    target = folders.create_folder(db, settings, name="Archive", parent_id=None)
    site = make_site(db, settings, seeded, "example.com")
    db.add(Job(type="capture", site_id=site.id, status="running", spec={}))
    db.flush()

    with pytest.raises(moves.SiteBusyError):
        moves.move_site(db, settings, site, target)


# ── deleting folders ─────────────────────────────────────────────────────


def test_deleting_a_folder_never_takes_archives_with_it(
    db: Session, settings: Settings, seeded: Folder
) -> None:
    holding = folders.create_folder(db, settings, name="Holding", parent_id=None)
    site = make_site(db, settings, holding, "example.com")

    with pytest.raises(folders.FolderError, match="still holds"):
        moves.delete_folder(db, settings, holding)

    moves.delete_folder(db, settings, holding, reassign_to=seeded.id)

    assert db.get(Folder, holding.id) is None
    assert site.folder_id == seeded.id
    assert storage.site_dir(settings, site.archive_path).is_dir()


def test_a_trashed_site_still_blocks_a_folder_delete(
    db: Session, settings: Settings, seeded: Folder
) -> None:
    """It still references the folder, so leaving it out of the count would
    produce a delete that fails at the database with no visible cause."""
    holding = folders.create_folder(db, settings, name="Holding", parent_id=None)
    site = make_site(db, settings, holding, "example.com")
    trash.trash_site(db, settings, site)

    with pytest.raises(folders.FolderError, match="in the trash"):
        moves.delete_folder(db, settings, holding)


# ── tags and the symlink tree ────────────────────────────────────────────


def test_the_tag_tree_is_relative_so_it_survives_being_read_over_smb(
    db: Session, settings: Settings, seeded: Folder
) -> None:
    """An absolute /data/... link means nothing on a client that mounted the
    share as Z:\\, and Samba refuses links that appear to leave the share."""
    if os.name == "nt":
        pytest.skip("symlinks need elevation on Windows; the tree is a Linux artefact")

    site = make_site(db, settings, seeded, "example.com")
    site_service.set_tags(db, site, ["Travel"])
    symlinks.rebuild(db, settings)

    link = settings.by_tag_dir / "travel" / "example-com"
    assert link.is_symlink()
    target = os.readlink(link)
    assert not os.path.isabs(target), f"{target} is absolute and would not resolve over SMB"
    assert link.resolve() == storage.site_dir(settings, site.archive_path)


def test_two_sites_with_one_slug_under_one_tag_both_get_their_id(
    db: Session, settings: Settings, seeded: Folder
) -> None:
    """Both, not just the newcomer — a name that depends on which row arrived
    first cannot be recomputed, and a tree that cannot be recomputed cannot be
    checked."""
    other = folders.create_folder(db, settings, name="Archive", parent_id=None)
    first = make_site(db, settings, seeded, "example.com")
    second = make_site(db, settings, other, "example.com")
    for site in (first, second):
        site_service.set_tags(db, site, ["travel"])

    names = set(symlinks.plan(db)["travel"])

    assert names == {f"example-com-{first.id}", f"example-com-{second.id}"}


def test_untagging_removes_the_link(db: Session, settings: Settings, seeded: Folder) -> None:
    if os.name == "nt":
        pytest.skip("symlinks need elevation on Windows")

    site = make_site(db, settings, seeded, "example.com")
    site_service.set_tags(db, site, ["travel", "food"])
    symlinks.rebuild(db, settings)
    assert (settings.by_tag_dir / "food" / "example-com").is_symlink()

    site_service.set_tags(db, site, ["travel"])
    symlinks.rebuild(db, settings)

    assert not (settings.by_tag_dir / "food").exists()
    assert (settings.by_tag_dir / "travel" / "example-com").is_symlink()


def test_a_link_is_never_created_before_its_target_exists(
    db: Session, settings: Settings, seeded: Folder
) -> None:
    """A symlink's type — file or directory — is fixed when it is created, and
    inferred from the target. Link first and it becomes a *file* link: Linux
    resolves it anyway, so nothing here notices, while a Windows-backed
    filesystem shows a 0 KB file for good.

    Asserted by watching `os.symlink` rather than by looking at the result,
    because the result is indistinguishable on the platform the tests run on.
    That is exactly why this needs a test at all.
    """
    calls: list[bool] = []
    real = os.symlink

    def spy(src: str, dst: object, **kwargs: object) -> None:
        # Relative source, resolved against the link's own directory.
        target = Path(os.path.normpath(os.path.join(os.path.dirname(str(dst)), str(src))))
        calls.append(target.is_dir())
        real(src, dst, **kwargs)  # type: ignore[arg-type]

    # Watching the whole of creation, not a rebuild afterwards: the bug was
    # that `create_site` linked before it made the directory, and by the time
    # anything else runs the directory is there and the evidence is gone.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(os, "symlink", spy)
        site_service.create_site(
            db, settings, seed_url="https://example.com/", folder_id=seeded.id, tags=["travel"]
        )

    assert calls, "creating a tagged site linked nothing at all"
    assert all(calls), "a link was created against a target that did not exist yet"


def test_rebuild_recreates_links_rather_than_trusting_the_text(
    db: Session, settings: Settings, seeded: Folder
) -> None:
    """The repair path for a link whose text is right and whose type is wrong.

    Leaving a matching link alone is the obvious optimisation and it is what
    made the mistyped-link bug unfixable — nothing on this side can see the
    type, so the only reliable repair is to make it again.
    """
    site = make_site(db, settings, seeded, "example.com")
    site_service.set_tags(db, site, ["travel"])
    symlinks.rebuild(db, settings)

    # Counting the syscall, not comparing inodes: a filesystem is free to hand
    # the same inode number back after an unlink, and overlayfs does — so the
    # first version of this test passed on Windows and failed in the container
    # while the code did exactly the same thing in both.
    calls: list[object] = []
    real = os.symlink

    def spy(src: str, dst: object, **kwargs: object) -> None:
        calls.append(dst)
        real(src, dst, **kwargs)  # type: ignore[arg-type]

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(os, "symlink", spy)
        symlinks.rebuild(db, settings)

    assert calls, "the link was left in place instead of remade"
    link = settings.by_tag_dir / "travel" / "example-com"
    if os.name != "nt":  # elsewhere the link never got written at all
        assert link.is_symlink()
        assert (link / "site.yaml").is_file()


def test_a_hand_made_directory_under_by_tag_is_never_deleted(
    db: Session, settings: Settings, seeded: Folder
) -> None:
    """Somebody's own directory on the share is not ours to remove because it
    is not in the database."""
    mine = settings.by_tag_dir / "travel" / "notes"
    mine.mkdir(parents=True)
    (mine / "readme.txt").write_text("mine", encoding="utf-8")

    symlinks.rebuild(db, settings)

    assert (mine / "readme.txt").read_text(encoding="utf-8") == "mine"


def test_tag_names_collapse_to_one_tag(db: Session, settings: Settings, seeded: Folder) -> None:
    site = make_site(db, settings, seeded, "example.com")

    applied = site_service.set_tags(db, site, ["Travel", "travel", "TRAVEL"])

    assert len(applied) == 1
    assert [row.tag.slug for row in tags.usage(db)] == ["travel"]


def test_a_bad_colour_is_refused_rather_than_corrected(db: Session) -> None:
    with pytest.raises(tags.TagError, match="not a colour"):
        tags.create(db, name="Travel", color="octarine", description=None)


# ── trash ────────────────────────────────────────────────────────────────


def test_delete_then_restore_returns_the_archive(
    db: Session, settings: Settings, seeded: Folder
) -> None:
    site = make_site(db, settings, seeded, "example.com")
    marker = storage.site_dir(settings, site.archive_path) / "captures" / "proof.txt"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("still here", encoding="utf-8")

    trash.trash_site(db, settings, site)
    assert not storage.site_dir(settings, site.archive_path).exists()
    assert trash.trash_path(settings, site).is_dir()

    trash.restore_site(db, settings, site)

    assert site.deleted_at is None
    restored = storage.site_dir(settings, site.archive_path) / "captures" / "proof.txt"
    assert restored.read_text(encoding="utf-8") == "still here"


def test_a_trashed_site_keeps_its_name_reserved(
    db: Session, settings: Settings, seeded: Folder
) -> None:
    """So a restore always gets its own path back.

    `UNIQUE(folder_id, slug)` spans deleted rows, and that is left alone
    deliberately. The visible cost is that deleting `example.com` and adding
    it again before purging gives the newcomer `example-com-2`. The
    alternative — freeing the name on delete — trades that for the case where
    a restored archive comes back under a suffixed name while a site created
    minutes ago holds its original one, which is the worse of the two: the
    thing that has been on disk for years is the one that should keep the
    name it has always had.
    """
    site = make_site(db, settings, seeded, "example.com")
    original = site.archive_path
    trash.trash_site(db, settings, site)

    replacement = make_site(db, settings, seeded, "example.com")
    assert replacement.slug == "example-com-2"

    trash.restore_site(db, settings, site)

    assert site.archive_path == original
    assert storage.site_dir(settings, original).is_dir()


def test_purge_removes_the_archive_and_the_row(
    db: Session, settings: Settings, seeded: Folder
) -> None:
    site = make_site(db, settings, seeded, "example.com")
    site_id = site.id
    (storage.site_dir(settings, site.archive_path) / "big.warc.gz").write_bytes(b"x" * 1024)

    trash.trash_site(db, settings, site)
    freed = trash.purge_site(db, settings, site)

    assert freed >= 1024
    assert db.get(Site, site_id) is None
    assert not (settings.trash_dir / f"{site_id}-example-com").exists()


def test_the_retention_sweep_only_takes_what_is_past_the_window(
    db: Session, settings: Settings, seeded: Folder
) -> None:
    from datetime import timedelta

    from cairn.db.types import utcnow
    from cairn.services import settings_store

    settings_store.put(db, trash.RETENTION_SETTING, 30)
    fresh = make_site(db, settings, seeded, "fresh.example")
    old = make_site(db, settings, seeded, "old.example")
    trash.trash_site(db, settings, fresh)
    trash.trash_site(db, settings, old)
    old.deleted_at = utcnow() - timedelta(days=45)
    db.flush()

    purged, _freed = trash.purge_expired(db, settings)

    assert purged == 1
    assert db.get(Site, fresh.id) is not None
    assert db.get(Site, old.id) is None


# ── through the API ──────────────────────────────────────────────────────


def test_the_folder_tree_rolls_sizes_up_into_ancestors(authed: TestClient) -> None:
    blogs = authed.post("/api/folders", json={"name": "Blogs"}, headers=XHR).json()
    photo = authed.post(
        "/api/folders", json={"name": "Photography", "parent_id": blogs["id"]}, headers=XHR
    ).json()
    authed.post(
        "/api/sites",
        json={"seed_url": "https://example.com/", "folder_id": photo["id"]},
        headers=XHR,
    )

    tree = authed.get("/api/folders", headers=XHR).json()
    node = next(f for f in tree if f["id"] == blogs["id"])

    assert node["site_count"] == 0
    assert node["total_site_count"] == 1, "a parent shows nothing of what its children hold"
    assert node["children"][0]["site_count"] == 1


def test_moving_a_site_through_the_api_reports_which_operation_it_was(
    authed: TestClient,
) -> None:
    target = authed.post("/api/folders", json={"name": "Archive"}, headers=XHR).json()
    site = authed.post("/api/sites", json={"seed_url": "https://example.com/"}, headers=XHR).json()

    moved = authed.post(
        f"/api/sites/{site['id']}/move", json={"folder_id": target["id"]}, headers=XHR
    )

    assert moved.status_code == 200, moved.text
    body = moved.json()
    assert body["status"] == "done"
    assert body["method"] == "rename"
    assert body["path"] == "Archive/example-com"


def test_bulk_tagging_reports_what_it_did(authed: TestClient) -> None:
    ids = [
        authed.post(
            "/api/sites", json={"seed_url": f"https://s{n}.example.com/"}, headers=XHR
        ).json()["id"]
        for n in range(3)
    ]

    result = authed.post(
        "/api/sites/bulk", json={"site_ids": ids, "add_tags": ["travel"]}, headers=XHR
    ).json()

    assert result["tagged"] == 3
    listed = authed.get("/api/sites?tag=travel", headers=XHR).json()
    assert listed["total"] == 3


def test_bulk_tagging_a_site_that_already_has_the_tag_does_not_fail_the_batch(
    authed: TestClient,
) -> None:
    """An IntegrityError inside the batch would take the whole transaction
    down, so nineteen sites would go untagged because one already was."""
    first = authed.post(
        "/api/sites", json={"seed_url": "https://a.example.com/", "tags": ["travel"]}, headers=XHR
    ).json()
    second = authed.post(
        "/api/sites", json={"seed_url": "https://b.example.com/"}, headers=XHR
    ).json()

    result = authed.post(
        "/api/sites/bulk",
        json={"site_ids": [first["id"], second["id"]], "add_tags": ["travel"]},
        headers=XHR,
    )

    assert result.status_code == 200, result.text
    assert result.json()["tagged"] == 1
