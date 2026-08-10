"""Startup sequence: back up, migrate, verify the key, seed defaults.

Called once from the FastAPI lifespan. Every step is idempotent — the
container restarts constantly during normal operation and none of this may
depend on being the first run.
"""

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from cairn.config import Settings
from cairn.crypto.sealing import key_fingerprint
from cairn.db.models import AccessProfile, Folder, Setting, User
from cairn.db.types import utcnow
from cairn.logging import get_logger

log = get_logger(__name__)

KEY_FINGERPRINT_SETTING = "secret_key_fingerprint"
DEFAULT_FOLDER_NAME = "Unfiled"
MAX_DB_BACKUPS = 10

# settings.value is NOT NULL and JsonText maps Python None to SQL NULL, so a
# setting must always carry a real JSON value. "Off" is expressed in the value
# ({"enabled": false}), never by absence — which also means reading a setting
# never has to distinguish "unset" from "set to nothing".
DEFAULT_SETTINGS: dict[str, object] = {
    "crawl.default_wait_s": 1.0,
    "crawl.default_rate_limit": "2m",
    "crawl.obey_robots": True,
    "crawl.user_agent": "Mozilla/5.0 (compatible; Cairn/1.0; +https://github.com/you/cairn)",
    "storage.trash_retention_days": 30,
    "storage.free_space_floor_bytes": 10 * 1024**3,
    "jobs.per_host_serial": True,
    "jobs.quiet_hours": {"enabled": False, "start": "01:00", "end": "07:00"},
    "notify.on_capture_failed": True,
    "notify.on_profile_expiring": True,
}


class KeyMismatchError(RuntimeError):
    """The master key changed or went missing while sealed data exists.

    Unrecoverable from inside the container: it needs the operator to supply
    the right key or explicitly re-key. The app exits with EXIT_CONFIG_ERROR
    so the supervisor stops the container instead of restarting forever.
    """


EXIT_CONFIG_ERROR = 78  # EX_CONFIG, per sysexits(3)


def _alembic_config(settings: Settings) -> Config:
    root = Path(__file__).resolve().parents[3]
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(Path(__file__).resolve().parent / "migrations"))
    cfg.set_main_option("sqlalchemy.url", settings.db_url)
    # env.py checks this: fileConfig() would replace the root log handlers and
    # every later application log would lose its JSON envelope and redaction.
    cfg.attributes["configure_logger"] = False
    return cfg


def backup_database(settings: Settings) -> Path | None:
    """Copy the database aside before migrating.

    Skipped when the database does not exist yet. Uses SQLite's backup API
    rather than a file copy — copying a WAL-mode database mid-write produces
    a file that restores as corrupt (docs/10).
    """
    if not settings.db_path.exists():
        return None

    import sqlite3

    settings.backups_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.strftime(utcnow(), "%Y%m%dT%H%M%SZ")
    target = settings.backups_dir / f"cairn-{stamp}.db"

    source = sqlite3.connect(settings.db_path)
    try:
        dest = sqlite3.connect(target)
        try:
            source.backup(dest)
        finally:
            dest.close()
    finally:
        source.close()

    backups = sorted(settings.backups_dir.glob("cairn-*.db"))
    for stale in backups[:-MAX_DB_BACKUPS]:
        stale.unlink(missing_ok=True)

    log.info("database backed up", extra={"path": str(target)})
    return target


def run_migrations(settings: Settings) -> None:
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    backup_database(settings)
    command.upgrade(_alembic_config(settings), "head")
    log.info("migrations applied")


def count_sealed_secrets(session: Session) -> int:
    """How many stored values would become unreadable under a different key."""
    totals = 0
    totals += session.scalar(select(func.count(User.id)).where(User.totp_secret.isnot(None))) or 0
    totals += (
        session.scalar(select(func.count(User.id)).where(User.recovery_codes.isnot(None))) or 0
    )
    totals += (
        session.scalar(
            select(func.count(AccessProfile.id)).where(
                AccessProfile.cookies_enc.isnot(None) | AccessProfile.script_enc.isnot(None)
            )
        )
        or 0
    )
    return totals


def verify_secret_key(session: Session, master_key: bytes) -> None:
    """Refuse to run under a different key than the one that sealed the data.

    The justification for refusing is that sealed values would silently become
    undecryptable — so when *nothing is sealed yet*, there is nothing to
    protect and refusing only blocks a legitimate setup step. Adding
    CAIRN_SECRET_KEY after a first run that auto-generated one is the normal
    path onto a supported configuration, and it used to leave the container
    crash-looping. Adopt the new key instead, and say so.
    """
    current = key_fingerprint(master_key)
    row = session.get(Setting, KEY_FINGERPRINT_SETTING)

    if row is None:
        session.add(Setting(key=KEY_FINGERPRINT_SETTING, value=current))
        session.flush()
        log.info("recorded master key fingerprint", extra={"fingerprint": current})
        return

    if row.value == current:
        return

    sealed = count_sealed_secrets(session)
    if sealed == 0:
        # Read the old value before overwriting it, or the log reports the new
        # fingerprint twice and the operator cannot tell what actually changed.
        previous = row.value
        row.value = current
        session.flush()
        log.warning(
            "Master key changed and nothing was sealed under the old one, so the new "
            "key has been adopted. If this was not deliberate, stop the container and "
            "restore the previous CAIRN_SECRET_KEY before storing any credentials.",
            extra={"was": previous, "now": current},
        )
        return

    raise KeyMismatchError(
        "CAIRN_SECRET_KEY does not match the key this database was created with.\n"
        f"  expected fingerprint: {row.value}\n"
        f"  got fingerprint:      {current}\n"
        f"  sealed values at risk: {sealed}\n"
        "\n"
        "Those values (2FA secrets, recovery codes, cookie jars) can only be read "
        "with the original key.\n"
        "\n"
        "  - Restore the original key: it is in /config/secret.key unless you moved\n"
        "    it into CAIRN_SECRET_KEY. Check with: cairn key-info\n"
        "  - Or accept losing them and re-key: cairn reset-key --force\n"
    )


def seed_defaults(session: Session) -> None:
    """Create the default folder and any missing settings. Idempotent."""
    existing = session.scalar(select(Folder).where(Folder.parent_id.is_(None)).limit(1))
    if existing is None:
        session.add(
            Folder(
                parent_id=None,
                name=DEFAULT_FOLDER_NAME,
                slug=DEFAULT_FOLDER_NAME.lower(),
                path=DEFAULT_FOLDER_NAME,
                sort_order=0,
            )
        )
        log.info("created default folder", extra={"folder": DEFAULT_FOLDER_NAME})

    known = set(session.scalars(select(Setting.key)).all())
    for key, value in DEFAULT_SETTINGS.items():
        if key not in known:
            session.add(Setting(key=key, value=value))


def ensure_directories(settings: Settings) -> None:
    for directory in (
        settings.config_dir,
        settings.engines_dir,
        settings.data_dir,
        settings.archives_dir,
        settings.personas_dir,
        settings.tmp_dir,
        settings.trash_dir,
        settings.by_tag_dir,
    ):
        directory.mkdir(parents=True, exist_ok=True)


def sweep_tmp(settings: Settings) -> None:
    """Delete leftovers from jobs that died mid-run.

    /data/tmp holds plaintext cookie jars materialized for running jobs. A
    crash leaves them on disk, so the window is closed at every boot
    (docs/06).
    """
    if not settings.tmp_dir.exists():
        return
    removed = 0
    for entry in settings.tmp_dir.iterdir():
        try:
            if entry.is_dir():
                shutil.rmtree(entry)
            else:
                entry.unlink()
            removed += 1
        except OSError as exc:  # pragma: no cover — permissions, races
            log.warning("could not remove temp entry", extra={"path": str(entry), "err": str(exc)})
    if removed:
        log.info("swept temp directory", extra={"entries": removed})


def warn_about_environment(settings: Settings, engine: Engine) -> None:
    """Log the misconfigurations that look fine until they very much aren't."""
    if settings.replay_shares_host_with_app():
        log.warning(
            "Replay is not isolated from the app by hostname. Ports do NOT isolate "
            "cookies, so archived JavaScript could read your session cookie. Set "
            "CAIRN_APP_PUBLIC_URL and CAIRN_REPLAY_PUBLIC_URL to different hostnames "
            "before exposing this instance to the internet. See docs/07."
        )

    # SQLite on Unraid's FUSE layer with mover active is the classic corruption
    # path (docs/10). Detect it cheaply and say so.
    db_str = str(settings.db_path).replace("\\", "/")
    if "/mnt/user/" in db_str:
        log.warning(
            "The database is under /mnt/user (Unraid's FUSE layer). SQLite locking "
            "is unreliable there and mover can relocate the file while it is open. "
            "Point /config at a cache-only share, e.g. /mnt/cache/appdata/cairn.",
            extra={"db_path": str(settings.db_path)},
        )

    with engine.connect() as conn:
        mode = conn.exec_driver_sql("PRAGMA journal_mode").scalar()
        if mode != "wal":  # pragma: no cover — only on exotic filesystems
            log.warning(
                "journal_mode is not WAL; concurrent reads will block writes.",
                extra={"journal_mode": mode},
            )
