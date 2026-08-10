"""Naming, atomic writes, and the path guards that keep engines in their lane."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cairn.config import Settings
from cairn.services import storage

# ── naming ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Example Blog", "example-blog"),
        ("  Spaces  Everywhere ", "spaces-everywhere"),
        ("Ünïcödé Títle", "unicode-title"),
        ("!!!", "site"),
        ("", "site"),
        ("a/b\\c", "a-b-c"),
        ("../../etc/passwd", "etc-passwd"),
    ],
)
def test_slugify(raw: str, expected: str) -> None:
    assert storage.slugify(raw) == expected


def test_slugify_never_produces_a_traversal_or_separator() -> None:
    slug = storage.slugify("../../../etc/passwd")
    assert "/" not in slug and "\\" not in slug and ".." not in slug


def test_slugify_avoids_windows_reserved_names() -> None:
    """The archive tree is routinely browsed over SMB, where a directory
    called `con` cannot be opened at all."""
    assert storage.slugify("CON") != "con"
    assert storage.slugify("lpt1") != "lpt1"


def test_unique_slug_appends_a_counter() -> None:
    assert storage.unique_slug("blog", set()) == "blog"
    assert storage.unique_slug("blog", {"blog"}) == "blog-2"
    assert storage.unique_slug("blog", {"blog", "blog-2"}) == "blog-3"


def test_capture_dir_name_is_sortable_and_utc() -> None:
    when = datetime(2026, 8, 9, 14, 25, 30, tzinfo=UTC)
    assert storage.capture_dir_name(when, "full", "wget-warc") == "20260809T142530Z-full-wget"


def test_capture_dir_name_converts_non_utc_input() -> None:
    """A local-time stamp labelled Z would misorder captures against UTC ones."""
    from datetime import timedelta, timezone

    when = datetime(2026, 8, 9, 16, 25, 30, tzinfo=timezone(timedelta(hours=2)))
    assert storage.capture_dir_name(when, "full", "wget-warc") == "20260809T142530Z-full-wget"


def test_capture_dir_names_round_trip_the_recognizer() -> None:
    name = storage.capture_dir_name(datetime.now(UTC), "incremental", "wget-warc")
    assert storage.is_capture_dir_name(name)
    assert not storage.is_capture_dir_name("random-directory")


# ── path safety ──────────────────────────────────────────────────────────


def test_resolve_within_allows_normal_paths(tmp_path: Path) -> None:
    assert storage.resolve_within(tmp_path, "warc/part-00000.warc.gz").is_relative_to(tmp_path)


@pytest.mark.parametrize(
    "escape", ["../outside", "warc/../../outside", "/etc/passwd", "..", "warc/../.."]
)
def test_resolve_within_rejects_escapes(tmp_path: Path, escape: str) -> None:
    """Artifact paths come from a subprocess whose output is data, not
    instruction."""
    with pytest.raises(storage.StoragePathError):
        storage.resolve_within(tmp_path, escape)


@pytest.mark.skipif(os.name == "nt", reason="symlink creation needs privileges on Windows")
def test_resolve_within_rejects_a_symlink_out(tmp_path: Path) -> None:
    base = tmp_path / "capture"
    base.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (base / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(storage.StoragePathError):
        storage.resolve_within(base, "link/secret")


# ── atomic writes ────────────────────────────────────────────────────────


def test_write_atomic_replaces_content(tmp_path: Path) -> None:
    target = tmp_path / "site.yaml"
    storage.write_atomic(target, "first")
    storage.write_atomic(target, "second")
    assert target.read_text() == "second"


def test_write_atomic_leaves_no_temp_file_behind(tmp_path: Path) -> None:
    target = tmp_path / "a" / "manifest.json"
    storage.write_json(target, {"schema": 1})
    assert [p.name for p in target.parent.iterdir()] == ["manifest.json"]


def test_write_atomic_cleans_up_when_writing_fails(tmp_path: Path) -> None:
    """A failed write must not leave a .tmp file that a later glob picks up."""

    class Unserializable:
        pass

    with pytest.raises(TypeError):
        storage.write_atomic(tmp_path / "x.json", Unserializable())  # type: ignore[arg-type]
    assert list(tmp_path.iterdir()) == []


def test_write_atomic_handles_bytes(tmp_path: Path) -> None:
    target = tmp_path / "blob.bin"
    storage.write_atomic(target, b"\x00\x01\x02")
    assert target.read_bytes() == b"\x00\x01\x02"


def test_yaml_roundtrip(tmp_path: Path) -> None:
    payload = {"schema": 1, "slug": "example", "tags": ["travel", "photo"]}
    path = tmp_path / "site.yaml"
    storage.write_yaml(path, payload)
    assert storage.read_yaml(path) == payload


# ── directories ──────────────────────────────────────────────────────────


def test_ensure_site_dirs_creates_the_full_layout(settings: Settings) -> None:
    root = storage.ensure_site_dirs(settings, "Unfiled/example-blog")
    for sub in ("captures", "index", "derived", "exports"):
        assert (root / sub).is_dir()


def test_ensure_capture_dirs_refuses_a_traversing_name(settings: Settings) -> None:
    storage.ensure_site_dirs(settings, "Unfiled/example-blog")
    with pytest.raises(storage.StoragePathError):
        storage.ensure_capture_dirs(settings, "Unfiled/example-blog", "../../escape")


def test_directory_size_counts_files(settings: Settings, tmp_path: Path) -> None:
    root = tmp_path / "site"
    (root / "warc").mkdir(parents=True)
    (root / "warc" / "a.warc.gz").write_bytes(b"x" * 100)
    (root / "warc" / "b.warc.gz").write_bytes(b"y" * 50)
    assert storage.directory_size(root) == 150


def test_directory_size_ignores_symlinks(tmp_path: Path) -> None:
    """/data/by-tag is a symlink tree into the archives; following it would
    count every tagged site once per tag."""
    if os.name == "nt":
        pytest.skip("symlink creation needs privileges on Windows")
    real = tmp_path / "real"
    real.mkdir()
    (real / "big.bin").write_bytes(b"x" * 1000)
    linked = tmp_path / "view"
    linked.mkdir()
    (linked / "link").symlink_to(real, target_is_directory=True)
    assert storage.directory_size(linked) == 0


def test_directory_size_of_a_missing_path_is_zero(tmp_path: Path) -> None:
    assert storage.directory_size(tmp_path / "nope") == 0
