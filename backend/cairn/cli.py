"""Command line entry point.

The UI is the primary interface (R1) — this exists for scripting, recovery,
and debugging, never as the only way to do something.
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import select

from cairn import __version__
from cairn.config import get_settings, load_or_create_secret_key
from cairn.crypto.sealing import key_fingerprint
from cairn.db.base import get_engine, sessionmaker_for
from cairn.db.bootstrap import ensure_directories, run_migrations, seed_defaults
from cairn.logging import configure_logging


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "cairn.app:app",
        factory=True,
        host=args.host or settings.host,
        port=args.port or settings.port,
        reload=args.reload,
        log_config=None,  # our logging config owns the handlers
        access_log=False,
    )
    return 0


def _cmd_migrate(_args: argparse.Namespace) -> int:
    settings = get_settings()
    ensure_directories(settings)
    run_migrations(settings)
    engine = get_engine(settings.db_url)
    with sessionmaker_for(engine)() as session:
        seed_defaults(session)
        session.commit()
    print("Migrations applied.")
    return 0


def _cmd_replay_init(_args: argparse.Namespace) -> int:
    """Write pywb's config and make the collection tree match the database.

    Runs before pywb starts, and by hand when a restore, a folder move or a
    recreated volume has left the tree disagreeing with the archives.
    """
    from cairn.services import replay

    settings = get_settings()
    ensure_directories(settings)
    config = replay.write_config(settings)

    engine = get_engine(settings.db_url)
    with sessionmaker_for(engine)() as session:
        linked, removed = replay.sync_collections(session, settings)

    print(f"Wrote {config}")
    print(f"Collections: {linked} linked, {removed} removed.")
    return 0


def _cmd_reindex(args: argparse.Namespace) -> int:
    from cairn.db.models import Site
    from cairn.services import replay

    settings = get_settings()
    engine = get_engine(settings.db_url)
    with sessionmaker_for(engine)() as session:
        query = select(Site).where(Site.deleted_at.is_(None))
        if args.slug:
            query = query.where(Site.slug == args.slug)
        sites = session.scalars(query).all()
        if not sites:
            print("No matching site." if args.slug else "No sites yet.")
            return 1
        for site in sites:
            result = replay.build_index(settings, site.archive_path)
            replay.link_collection(settings, site.id, site.archive_path)
            print(f"{site.slug}: {result.records} record(s) from {result.warcs} WARC(s)")
    return 0


def _cmd_rebuild_symlinks(_args: argparse.Namespace) -> int:
    """Regenerate `/data/by-tag` from the database.

    The repair for a tag tree that no longer matches — after a restore, a
    volume recreated empty, or somebody tidying up over the share.
    """
    from cairn.services import symlinks

    settings = get_settings()
    ensure_directories(settings)
    engine = get_engine(settings.db_url)
    with sessionmaker_for(engine)() as session:
        linked, removed = symlinks.rebuild(session, settings)
    print(f"Tag tree: {linked} link(s) written, {removed} removed.")
    return 0


def _cmd_purge_trash(args: argparse.Namespace) -> int:
    """Purge deleted sites. Not reversible."""
    from cairn.services import trash

    settings = get_settings()
    engine = get_engine(settings.db_url)
    with sessionmaker_for(engine)() as session:
        if args.all:
            entries = trash.list_trash(session, settings)
            freed = sum(trash.purge_site(session, settings, row.site) for row in entries)
            purged = len(entries)
        else:
            purged, freed = trash.purge_expired(session, settings)
        session.commit()
        window = trash.retention_days(session)

    if args.all:
        print(f"Purged all {purged} trashed site(s), freeing {freed:,} bytes.")
    else:
        print(f"Purged {purged} site(s) older than {window} day(s), freeing {freed:,} bytes.")
    return 0


def _read_new_password(from_stdin: bool) -> str | None:
    """Prompt for a password, or read one line from stdin.

    `--stdin` exists because getpass needs a TTY, and some consoles (notably
    Unraid's browser terminal, and `docker exec` without -it) do not give you
    one.
    """
    import getpass

    if from_stdin:
        password = sys.stdin.readline().rstrip("\n")
        return password or None

    password = getpass.getpass("New password: ")
    if password != getpass.getpass("Confirm: "):
        print("Passwords did not match.", file=sys.stderr)
        return None
    return password


def _cmd_reset_password(args: argparse.Namespace) -> int:
    """Recovery path for a lost password. Requires filesystem access."""
    from cairn.crypto.passwords import hash_password, validate_password_strength
    from cairn.services import audit, auth

    settings = get_settings()
    engine = get_engine(settings.db_url)
    with sessionmaker_for(engine)() as session:
        user = auth.get_user(session, args.username)
        if user is None:
            print(f"No such user: {args.username}", file=sys.stderr)
            return 1

        password = _read_new_password(args.stdin)
        if not password:
            print("No password provided.", file=sys.stderr)
            return 1

        problems = validate_password_strength(password, settings.password_min_length)
        if problems:
            for problem in problems:
                print(f"  - {problem}", file=sys.stderr)
            return 1

        user.password_hash = hash_password(password)
        revoked = auth.revoke_all_sessions(session, user.id)
        # You reset a password *because* you are locked out. Leaving the
        # lockout in place would keep you out for up to an hour afterwards.
        cleared = auth.clear_lockout(session, user)
        audit.record(session, audit.PASSWORD_CHANGED, actor=user.username, detail={"via": "cli"})
        session.commit()

    print(f"Password updated. {revoked} session(s) revoked, {cleared} failed attempt(s) cleared.")
    return 0


def _cmd_unlock(args: argparse.Namespace) -> int:
    """Clear a rate-limit lockout without changing the password."""
    from cairn.services import audit, auth

    settings = get_settings()
    engine = get_engine(settings.db_url)
    with sessionmaker_for(engine)() as session:
        user = auth.get_user(session, args.username)
        if user is None:
            print(f"No such user: {args.username}", file=sys.stderr)
            return 1
        cleared = auth.clear_lockout(session, user)
        audit.record(session, audit.ACCOUNT_UNLOCKED, actor=user.username, detail={"via": "cli"})
        session.commit()

    print(f"Unlocked {args.username}. {cleared} failed attempt(s) cleared.")
    return 0


def _cmd_list_users(_args: argparse.Namespace) -> int:
    """Show whether setup has run, and whether the account is locked."""
    from sqlalchemy import select

    from cairn.db.models import User
    from cairn.db.types import utcnow

    settings = get_settings()
    engine = get_engine(settings.db_url)
    with sessionmaker_for(engine)() as session:
        users = list(session.scalars(select(User)).all())
        if not users:
            print("No account exists yet. Open the web UI to create one.")
            return 0
        for user in users:
            locked = user.locked_until is not None and user.locked_until > utcnow()
            state = "LOCKED until " + str(user.locked_until) if locked else "ok"
            totp = "2FA on" if user.totp_enabled else "2FA off"
            print(f"{user.username}\t{state}\t{totp}\tlast login: {user.last_login_at or 'never'}")
    return 0


def _cmd_disable_totp(args: argparse.Namespace) -> int:
    """Recovery path for a lost authenticator device."""
    from cairn.services import audit, auth

    settings = get_settings()
    engine = get_engine(settings.db_url)
    with sessionmaker_for(engine)() as session:
        user = auth.get_user(session, args.username)
        if user is None:
            print(f"No such user: {args.username}", file=sys.stderr)
            return 1
        user.totp_secret = None
        user.totp_confirmed = False
        user.recovery_codes = None
        audit.record(session, audit.TOTP_DISABLED, actor=user.username, detail={"via": "cli"})
        session.commit()
    print("Two-factor authentication disabled.")
    return 0


def _cmd_reset_key(args: argparse.Namespace) -> int:
    """Adopt the current key, discarding anything sealed under the old one.

    The deliberate escape hatch for "I lost the old CAIRN_SECRET_KEY". It
    destroys data, so it requires --force rather than a prompt: this is
    usually run through `docker exec`, where a prompt may have no TTY.
    """
    from cairn.crypto.sealing import key_fingerprint
    from cairn.db.bootstrap import KEY_FINGERPRINT_SETTING, count_sealed_secrets
    from cairn.db.models import AccessProfile, Setting, User

    settings = get_settings()
    engine = get_engine(settings.db_url)
    with sessionmaker_for(engine)() as session:
        sealed = count_sealed_secrets(session)
        if not args.force:
            print(f"{sealed} sealed value(s) would be destroyed:", file=sys.stderr)
            print("  2FA secrets, recovery codes and stored cookie jars.", file=sys.stderr)
            print("Re-run with --force to proceed.", file=sys.stderr)
            return 1

        for user in session.scalars(select(User)).all():
            user.totp_secret = None
            user.totp_confirmed = False
            user.recovery_codes = None
        for profile in session.scalars(select(AccessProfile)).all():
            profile.cookies_enc = None
            profile.script_enc = None
            profile.minted_at = None
            profile.last_verify_result = "needs_reauth"

        current = key_fingerprint(load_or_create_secret_key(settings))
        row = session.get(Setting, KEY_FINGERPRINT_SETTING)
        if row is None:
            session.add(Setting(key=KEY_FINGERPRINT_SETTING, value=current))
        else:
            row.value = current
        session.commit()

    print(f"Re-keyed. {sealed} sealed value(s) cleared; two-factor auth is now off.")
    print("Sign in with your password and set 2FA up again.")
    return 0


def _cmd_key_info(_args: argparse.Namespace) -> int:
    settings = get_settings()
    ensure_directories(settings)
    key = load_or_create_secret_key(settings)
    print(f"fingerprint: {key_fingerprint(key)}")
    print(f"source:      {'CAIRN_SECRET_KEY' if settings.secret_key else settings.secret_key_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cairn", description="Self-hosted website archiver.")
    parser.add_argument("--version", action="version", version=f"cairn {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="Run the web server")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    serve.add_argument("--reload", action="store_true")
    serve.set_defaults(func=_cmd_serve)

    sub.add_parser("migrate", help="Apply database migrations").set_defaults(func=_cmd_migrate)

    sub.add_parser(
        "replay-init", help="Write pywb's config and rebuild the collection tree"
    ).set_defaults(func=_cmd_replay_init)

    sub.add_parser(
        "rebuild-symlinks", help="Regenerate /data/by-tag from the database"
    ).set_defaults(func=_cmd_rebuild_symlinks)

    purge = sub.add_parser("purge-trash", help="Delete trashed sites past the retention window")
    purge.add_argument(
        "--all", action="store_true", help="Purge everything in the trash, whatever its age"
    )
    purge.set_defaults(func=_cmd_purge_trash)

    reindex = sub.add_parser("reindex", help="Rebuild the replay index for one site or all sites")
    reindex.add_argument("slug", nargs="?", help="Site slug; omit for every site")
    reindex.set_defaults(func=_cmd_reindex)

    reset = sub.add_parser(
        "reset-password", help="Reset a user's password (also clears any lockout)"
    )
    reset.add_argument("username")
    reset.add_argument(
        "--stdin",
        action="store_true",
        help="Read the new password from stdin instead of prompting (no TTY needed)",
    )
    reset.set_defaults(func=_cmd_reset_password)

    unlock = sub.add_parser("unlock", help="Clear a rate-limit lockout, keeping the password")
    unlock.add_argument("username")
    unlock.set_defaults(func=_cmd_unlock)

    disable = sub.add_parser("disable-totp", help="Turn off 2FA for a user")
    disable.add_argument("username")
    disable.set_defaults(func=_cmd_disable_totp)

    sub.add_parser("users", help="List accounts and whether they are locked").set_defaults(
        func=_cmd_list_users
    )

    sub.add_parser("key-info", help="Show the master key fingerprint").set_defaults(
        func=_cmd_key_info
    )

    rekey = sub.add_parser(
        "reset-key", help="Adopt the current key, discarding values sealed under the old one"
    )
    rekey.add_argument("--force", action="store_true", help="Required; this destroys data")
    rekey.set_defaults(func=_cmd_reset_key)

    args = parser.parse_args(argv)
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    result: int = args.func(args)
    return result


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
