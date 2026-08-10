"""Startup: migrations, key verification, seeding, restart safety."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect, select

from cairn.config import Settings
from cairn.crypto.sealing import Sealer, key_fingerprint
from cairn.db.base import get_engine, sessionmaker_for
from cairn.db.bootstrap import (
    KEY_FINGERPRINT_SETTING,
    KeyMismatchError,
    count_sealed_secrets,
    ensure_directories,
    run_migrations,
    seed_defaults,
    sweep_tmp,
    verify_secret_key,
)
from cairn.db.models import AccessProfile, Folder, Setting, User
from cairn.db.types import utcnow
from tests.conftest import PASSWORD, TEST_KEY, USERNAME, XHR

EXPECTED_TABLES = {
    "users", "sessions", "login_attempts", "audit_log", "folders", "tags",
    "site_tags", "sites", "discoveries", "discovered_hosts", "scope_rules",
    "scope_patterns", "captures", "capture_urls", "feeds", "feed_items",
    "access_profiles", "jobs", "engines", "settings", "saved_views",
}  # fmt: skip


def test_migration_creates_the_whole_schema(settings: Settings) -> None:
    ensure_directories(settings)
    run_migrations(settings)
    tables = set(inspect(get_engine(settings.db_url)).get_table_names())
    assert tables >= EXPECTED_TABLES


def test_migrations_are_idempotent(settings: Settings) -> None:
    """The container restarts constantly; a second run must be a no-op."""
    ensure_directories(settings)
    run_migrations(settings)
    run_migrations(settings)  # must not raise


def test_wal_and_foreign_keys_enabled(settings: Settings) -> None:
    ensure_directories(settings)
    run_migrations(settings)
    with get_engine(settings.db_url).connect() as conn:
        assert conn.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
        # OFF by default in SQLite — without it, ON DELETE is decorative.
        assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1


def test_seed_defaults_is_idempotent(settings: Settings) -> None:
    ensure_directories(settings)
    run_migrations(settings)
    factory = sessionmaker_for(get_engine(settings.db_url))
    with factory() as session:
        seed_defaults(session)
        session.commit()
        seed_defaults(session)
        session.commit()
        assert len(session.scalars(select(Folder)).all()) == 1
        assert len(session.scalars(select(Setting)).all()) >= 1


def test_key_fingerprint_recorded_on_first_run(settings: Settings) -> None:
    ensure_directories(settings)
    run_migrations(settings)
    factory = sessionmaker_for(get_engine(settings.db_url))

    with factory() as session:
        verify_secret_key(session, TEST_KEY.encode())
        session.commit()
        assert session.get(Setting, KEY_FINGERPRINT_SETTING).value == key_fingerprint(
            TEST_KEY.encode()
        )


def test_key_adoption_logs_both_fingerprints(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    """The warning must show what changed. Reading row.value after assigning
    it reports the new fingerprint twice, which tells the operator nothing."""
    ensure_directories(settings)
    run_migrations(settings)
    factory = sessionmaker_for(get_engine(settings.db_url))
    new_key = b"a-completely-different-key-32bytes!"

    with factory() as session:
        verify_secret_key(session, TEST_KEY.encode())
        session.commit()

    with factory() as session, caplog.at_level("WARNING"):
        verify_secret_key(session, new_key)
        session.commit()

    record = next(r for r in caplog.records if "adopted" in r.getMessage())
    assert record.was == key_fingerprint(TEST_KEY.encode())  # type: ignore[attr-defined]
    assert record.now == key_fingerprint(new_key)  # type: ignore[attr-defined]
    assert record.was != record.now  # type: ignore[attr-defined]


def test_key_change_is_adopted_when_nothing_is_sealed(settings: Settings) -> None:
    """Setting CAIRN_SECRET_KEY after a run that auto-generated one is the
    normal path onto a supported configuration. Refusing there protects
    nothing — no sealed value exists — and only leaves the container dead."""
    ensure_directories(settings)
    run_migrations(settings)
    factory = sessionmaker_for(get_engine(settings.db_url))

    with factory() as session:
        verify_secret_key(session, TEST_KEY.encode())
        session.commit()

    new_key = b"a-completely-different-key-32bytes!"
    with factory() as session:
        verify_secret_key(session, new_key)  # must not raise
        session.commit()

    with factory() as session:
        assert session.get(Setting, KEY_FINGERPRINT_SETTING).value == key_fingerprint(new_key)


def test_key_change_is_fatal_once_something_is_sealed(settings: Settings) -> None:
    """With real sealed data, silently adopting a new key would destroy it."""
    ensure_directories(settings)
    run_migrations(settings)
    factory = sessionmaker_for(get_engine(settings.db_url))
    sealer = Sealer(TEST_KEY.encode())

    with factory() as session:
        verify_secret_key(session, TEST_KEY.encode())
        session.add(
            User(
                username="admin",
                password_hash="x",
                created_at=utcnow(),
                totp_secret=sealer.seal("SECRET", context="user.totp"),
                totp_confirmed=True,
            )
        )
        session.commit()

    with factory() as session:
        assert count_sealed_secrets(session) == 1
        with pytest.raises(KeyMismatchError, match="does not match"):
            verify_secret_key(session, b"a-completely-different-key-32bytes!")


def test_sealed_secret_count_covers_every_sealed_column(settings: Settings) -> None:
    """If a new sealed column is added without updating this count, the
    key-change guard silently stops protecting it."""
    ensure_directories(settings)
    run_migrations(settings)
    factory = sessionmaker_for(get_engine(settings.db_url))
    sealer = Sealer(TEST_KEY.encode())

    with factory() as session:
        assert count_sealed_secrets(session) == 0
        session.add(
            User(
                username="admin",
                password_hash="x",
                created_at=utcnow(),
                totp_secret=sealer.seal("a", context="user.totp"),
                recovery_codes=sealer.seal("b", context="user.recovery"),
            )
        )
        session.add(
            AccessProfile(
                name="blogger",
                mode="cookies",
                cookies_enc=sealer.seal("jar", context="profile.cookies"),
                created_at=utcnow(),
                updated_at=utcnow(),
            )
        )
        session.commit()
        # user.totp_secret + user.recovery_codes + one profile
        assert count_sealed_secrets(session) == 3


def test_tmp_sweep_removes_leftover_material(settings: Settings) -> None:
    """Plaintext cookie jars from crashed jobs must not survive a restart."""
    ensure_directories(settings)
    leftover = settings.tmp_dir / "job-512"
    leftover.mkdir(parents=True)
    (leftover / "cookies.txt").write_text("secret jar")
    stray = settings.tmp_dir / "loose.txt"
    stray.write_text("x")

    sweep_tmp(settings)

    assert not leftover.exists()
    assert not stray.exists()
    assert settings.tmp_dir.exists()


def test_session_survives_a_restart(settings: Settings) -> None:
    """M0's headline exit criterion."""
    from cairn.app import create_app

    with TestClient(create_app(settings)) as client:
        client.post("/api/setup", json={"username": USERNAME, "password": PASSWORD}, headers=XHR)
        cookie = client.cookies.get("cairn_session")

    with TestClient(create_app(settings)) as client:
        client.cookies.set("cairn_session", cookie or "")
        res = client.get("/api/auth/me")
        assert res.status_code == 200
        assert res.json()["username"] == USERNAME


def test_database_backed_up_before_migrating(settings: Settings) -> None:
    ensure_directories(settings)
    run_migrations(settings)  # creates the db
    run_migrations(settings)  # should now back it up first
    assert list(settings.backups_dir.glob("cairn-*.db")), "no backup was taken"
