"""Command line entry point.

The UI is the primary interface (R1) — this exists for scripting, recovery,
and debugging, never as the only way to do something.
"""

from __future__ import annotations

import argparse
import sys

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


def _cmd_reset_password(args: argparse.Namespace) -> int:
    """Recovery path for a lost password. Requires filesystem access."""
    import getpass

    from cairn.crypto.passwords import hash_password, validate_password_strength
    from cairn.services import audit, auth

    settings = get_settings()
    engine = get_engine(settings.db_url)
    with sessionmaker_for(engine)() as session:
        user = auth.get_user(session, args.username)
        if user is None:
            print(f"No such user: {args.username}", file=sys.stderr)
            return 1

        password = getpass.getpass("New password: ")
        if password != getpass.getpass("Confirm: "):
            print("Passwords did not match.", file=sys.stderr)
            return 1

        problems = validate_password_strength(password, settings.password_min_length)
        if problems:
            for problem in problems:
                print(f"  - {problem}", file=sys.stderr)
            return 1

        user.password_hash = hash_password(password)
        revoked = auth.revoke_all_sessions(session, user.id)
        audit.record(session, audit.PASSWORD_CHANGED, actor=user.username, detail={"via": "cli"})
        session.commit()

    print(f"Password updated. {revoked} session(s) revoked.")
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

    reset = sub.add_parser("reset-password", help="Reset a user's password")
    reset.add_argument("username")
    reset.set_defaults(func=_cmd_reset_password)

    disable = sub.add_parser("disable-totp", help="Turn off 2FA for a user")
    disable.add_argument("username")
    disable.set_defaults(func=_cmd_disable_totp)

    sub.add_parser("key-info", help="Show the master key fingerprint").set_defaults(
        func=_cmd_key_info
    )

    args = parser.parse_args(argv)
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    result: int = args.func(args)
    return result


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
