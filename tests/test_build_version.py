"""Reporting which build is running.

This exists because a version string alone answered the question wrongly: an
image several milestones behind reported the same "0.1.0" as the tree it was
being compared against, and a capture was diagnosed against code that was not
running. The build id is the part that has to change.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cairn import __version__
from cairn.build import UNKNOWN_BUILD, build_info

XHR = {"X-Requested-With": "XMLHttpRequest"}


@pytest.fixture(autouse=True)
def _fresh_build_info() -> None:
    """`build_info` is cached for the process; each test wants its own answer."""
    build_info.cache_clear()


def test_environment_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CAIRN_BUILD", "a9873aa")
    monkeypatch.setenv("CAIRN_BUILT_AT", "2026-08-10T21:00:00Z")

    info = build_info()

    assert info.version == __version__
    assert info.build == "a9873aa"
    assert info.built_at == "2026-08-10T21:00:00Z"
    assert info.label == f"{__version__} (a9873aa)"


def test_build_info_file_is_used_when_the_environment_is_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """What a plain `docker build` leaves behind: two lines, id then timestamp."""
    monkeypatch.delenv("CAIRN_BUILD", raising=False)
    monkeypatch.delenv("CAIRN_BUILT_AT", raising=False)
    stamp = tmp_path / "BUILD_INFO"
    stamp.write_text("img-2608102100\n2026-08-10T21:00:00Z\n", encoding="utf-8")
    monkeypatch.setattr("cairn.build.BUILD_INFO_PATH", stamp)

    info = build_info()

    assert info.build == "img-2608102100"
    assert info.built_at == "2026-08-10T21:00:00Z"


def test_a_source_checkout_never_invents_a_build(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With no stamp and no git, say so. A fabricated id is worse than none —
    it would read as a real build that nobody can find."""
    monkeypatch.delenv("CAIRN_BUILD", raising=False)
    monkeypatch.delenv("CAIRN_BUILT_AT", raising=False)
    monkeypatch.setattr("cairn.build.BUILD_INFO_PATH", tmp_path / "absent")
    monkeypatch.setattr("cairn.build._git_describe", lambda: None)

    info = build_info()

    assert info.build == UNKNOWN_BUILD
    assert info.built_at is None


def test_version_endpoint_requires_a_session(client: TestClient) -> None:
    """The build names the commit, which names the known bugs. That is not
    something an unauthenticated liveness probe hands out (docs/11)."""
    assert client.get("/api/version").status_code == 401


def test_version_endpoint_reports_the_running_build(
    authed: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CAIRN_BUILD", "deadbee")
    build_info.cache_clear()

    body = authed.get("/api/version", headers=XHR).json()

    assert body["version"] == __version__
    assert body["build"] == "deadbee"
    assert body["label"] == f"{__version__} (deadbee)"


def test_health_still_leaks_nothing_beyond_the_version(client: TestClient) -> None:
    body = client.get("/api/health").json()
    assert body["version"] == __version__
    assert "build" not in body
