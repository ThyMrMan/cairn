"""The port a browser is told to use for replay.

Reported from a real Unraid install: the replay tab loaded nothing whenever the
replay port had been changed in the template. `replay_port` is the port pywb
*binds to inside the container*, and it was being handed to the browser as the
port to *connect to* — which are the same number only when the container port
is published unchanged. Unraid's template edits the host side of that mapping,
so changing it is the normal thing to do and it broke replay silently.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cairn.config import Settings

BASE: dict[str, object] = {
    "secret_key": "test-master-key-must-be-at-least-32-bytes-long",
    "_env_file": None,
}


def _settings(tmp_path: Path, **extra: object) -> Settings:
    return Settings(
        config_dir=tmp_path / "config",
        data_dir=tmp_path / "data",
        **BASE,  # type: ignore[arg-type]
        **extra,  # type: ignore[arg-type]
    )


def test_the_bind_port_is_used_when_nothing_says_otherwise(tmp_path: Path) -> None:
    """A 1:1 mapping is the common case and must keep working untouched."""
    settings = _settings(tmp_path)
    assert settings.replay_origin_for("http", "box.local") == "http://box.local:8081"


def test_a_published_port_that_differs_from_the_bind_port_is_honoured(tmp_path: Path) -> None:
    """`-p 9081:8081`: pywb still binds 8081, the browser must be sent to 9081."""
    settings = _settings(tmp_path, replay_public_port=9081)
    assert settings.replay_origin_for("http", "box.local") == "http://box.local:9081"
    # And the container's own path to pywb is unchanged — the thumbnail
    # screenshotter talks to the sidecar, not through the host mapping.
    assert settings.replay_internal_origin == "http://127.0.0.1:8081"


def test_a_full_public_url_still_wins(tmp_path: Path) -> None:
    """Behind a reverse proxy the hostname changes too, so the URL beats a port."""
    settings = _settings(
        tmp_path,
        replay_public_port=9081,
        replay_public_url="https://replay.example.com",
    )
    assert (
        settings.replay_origin_for("https", "archive.example.com") == "https://replay.example.com"
    )


@pytest.mark.parametrize(
    ("external_port", "assumed"),
    [
        # Reached on the port it binds: no remapping, nothing to warn about.
        (8080, False),
        # Reached on something else: ports are being remapped, so the replay
        # port we are about to hand out is a guess.
        (8087, True),
        (443, True),
    ],
)
def test_the_guess_is_flagged_only_when_ports_are_being_remapped(
    tmp_path: Path, external_port: int, assumed: bool
) -> None:
    assert _settings(tmp_path).replay_port_is_assumed(external_port) is assumed


def test_nothing_is_assumed_once_it_has_been_told(tmp_path: Path) -> None:
    """The warning must go away when the operator answers it, or it is noise
    that teaches people to ignore warnings."""
    told_port = _settings(tmp_path, replay_public_port=9081)
    assert told_port.replay_port_is_assumed(8087) is False

    told_url = _settings(tmp_path, replay_public_url="https://replay.example.com")
    assert told_url.replay_port_is_assumed(8087) is False


def test_the_replay_tab_reports_the_port_it_is_using(authed, db, settings) -> None:
    """The status endpoint carries both the number and whether it is a guess,
    because a wrong one is a blank iframe explained only in the console."""
    from cairn.db.models import Site
    from cairn.services import storage

    site = Site(
        folder_id=1,
        slug="s",
        title="S",
        seed_url="http://blog.test/",
        primary_host="blog.test",
        archive_path="Unfiled/s",
    )
    db.add(site)
    db.flush()
    storage.ensure_site_dirs(settings, site.archive_path)
    db.commit()

    body = authed.get(f"/api/sites/{site.id}/replay").json()
    assert body["replay_port"] == settings.replay_port

    # The test client arrives at `http://testserver/` with no port, which reads
    # as 80 — genuinely different from the 8080 the app binds, so the flag is
    # up. That is the same answer a real reverse-proxied install gets, and it
    # is the right one: on :443 the replay port is exactly as unknowable, and
    # such a deployment is supposed to set the public URL.
    assert body["port_is_assumed"] is True


def test_the_flag_clears_once_the_port_is_configured(authed, db, settings, monkeypatch) -> None:
    """The other half of the previous test: answering the question silences it."""
    from cairn.db.models import Site
    from cairn.services import storage

    site = Site(
        folder_id=1,
        slug="t",
        title="T",
        seed_url="http://blog.test/",
        primary_host="blog.test",
        archive_path="Unfiled/t",
    )
    db.add(site)
    db.flush()
    storage.ensure_site_dirs(settings, site.archive_path)
    db.commit()

    monkeypatch.setattr(settings, "replay_public_port", 9081)
    body = authed.get(f"/api/sites/{site.id}/replay").json()
    assert body["port_is_assumed"] is False
    assert body["replay_port"] == 9081
    assert body["base_url"].startswith("http://testserver:9081/")
