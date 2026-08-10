"""Application settings.

Three tiers, later overriding earlier (docs/02):
  1. defaults in code
  2. CAIRN_* environment variables — deployment-level, restart required
  3. database settings — everything a user can change without a restart

The rule: if changing it requires a container restart, it belongs here.
Anything else belongs in the `settings` table.
"""

from __future__ import annotations

import secrets
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

SECRET_KEY_FILE = "secret.key"  # noqa: S105 — a filename, not a credential
SECRET_KEY_MIN_BYTES = 32


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CAIRN_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── paths ────────────────────────────────────────────────────────────
    data_dir: Path = Path("/data")
    config_dir: Path = Path("/config")

    # ── network ──────────────────────────────────────────────────────────
    host: str = "0.0.0.0"  # noqa: S104 — container-internal; exposure is the operator's choice
    port: int = 8080
    replay_port: int = 8081
    replay_public_url: str = ""
    app_public_url: str = ""

    # ── secrets ──────────────────────────────────────────────────────────
    secret_key: str = ""

    # ── auth ─────────────────────────────────────────────────────────────
    session_idle_days: int = 7
    session_absolute_days: int = 30
    login_max_attempts: int = 5
    login_window_seconds: int = 900
    login_lockout_seconds: int = 3600
    password_min_length: int = 12
    cookie_secure: bool | None = None  # None => infer from public URL scheme
    cookie_name: str = "cairn_session"

    # ── proxy / external auth ────────────────────────────────────────────
    trusted_proxy: str = ""
    auth_header_mode: bool = False
    auth_header_name: str = "Remote-User"

    # ── jobs ─────────────────────────────────────────────────────────────
    max_concurrent_jobs: int = 2

    # ── misc ─────────────────────────────────────────────────────────────
    log_level: str = "INFO"
    log_json: bool = True
    dev_mode: bool = False

    # ── derived ──────────────────────────────────────────────────────────

    @property
    def db_path(self) -> Path:
        return self.config_dir / "cairn.db"

    @property
    def db_url(self) -> str:
        return f"sqlite+pysqlite:///{self.db_path.as_posix()}"

    @property
    def secret_key_path(self) -> Path:
        return self.config_dir / SECRET_KEY_FILE

    @property
    def archives_dir(self) -> Path:
        return self.data_dir / "archives"

    @property
    def personas_dir(self) -> Path:
        return self.data_dir / "personas"

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"

    @property
    def trash_dir(self) -> Path:
        return self.data_dir / "trash"

    @property
    def by_tag_dir(self) -> Path:
        return self.data_dir / "by-tag"

    @property
    def backups_dir(self) -> Path:
        return self.config_dir / "backups"

    @property
    def engines_dir(self) -> Path:
        return self.config_dir / "engines"

    @property
    def use_secure_cookies(self) -> bool:
        """Secure cookies unless we can tell we are on plain HTTP."""
        if self.cookie_secure is not None:
            return self.cookie_secure
        if self.app_public_url:
            return urlsplit(self.app_public_url).scheme == "https"
        # No public URL configured — almost certainly a LAN/localhost install
        # over plain HTTP, where Secure cookies would never be sent back.
        return False

    @property
    def replay_origin(self) -> str:
        """External origin for replayed content, used for CSP frame-src."""
        if self.replay_public_url:
            return self.replay_public_url.rstrip("/")
        return ""

    def replay_shares_host_with_app(self) -> bool:
        """True when replay is not isolated from the app by hostname.

        Ports do not isolate cookies: app.example.com:8080 and
        app.example.com:8081 share a cookie jar, so archived JavaScript could
        read the session cookie (docs/07, docs/11). Callers warn loudly.
        """
        if not self.app_public_url or not self.replay_public_url:
            return True
        return urlsplit(self.app_public_url).hostname == urlsplit(self.replay_public_url).hostname

    # ── validation ───────────────────────────────────────────────────────

    @field_validator("log_level")
    @classmethod
    def _upper_log_level(cls, v: str) -> str:
        level = v.upper()
        if level not in {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}:
            raise ValueError(f"invalid log level: {v}")
        return level

    @field_validator("data_dir", "config_dir")
    @classmethod
    def _absolute_paths(cls, v: Path) -> Path:
        return v.expanduser().resolve()

    @model_validator(mode="after")
    def _check_auth_header_mode(self) -> Settings:
        # Trusted-header auth without a trusted proxy means anyone can send the
        # header and walk in. Refuse rather than start insecurely (docs/11).
        if self.auth_header_mode and not self.trusted_proxy:
            raise ValueError(
                "CAIRN_AUTH_HEADER_MODE is on but CAIRN_TRUSTED_PROXY is unset. "
                "Without a trusted proxy, anyone can forge the auth header. "
                "Set CAIRN_TRUSTED_PROXY to the proxy address, or disable header auth."
            )
        return self


class SecretKeyError(RuntimeError):
    """Raised when the master key is missing, weak, or has changed."""


def load_or_create_secret_key(settings: Settings) -> bytes:
    """Return the master key, generating one on first run.

    Precedence: CAIRN_SECRET_KEY env var, then /config/secret.key.

    A generated key is written to /config/secret.key with mode 600 and logged
    prominently. Refusing to start when sealed secrets exist but the key is
    gone is handled by `cairn.crypto.sealing.verify_key_matches` — silently
    starting with a fresh key would leave every stored cookie jar
    undecryptable with no indication of why (docs/10).
    """
    from cairn.logging import get_logger

    log = get_logger(__name__)

    if settings.secret_key:
        raw = settings.secret_key.encode("utf-8")
        if len(raw) < SECRET_KEY_MIN_BYTES:
            raise SecretKeyError(
                f"CAIRN_SECRET_KEY is too short ({len(raw)} bytes); "
                f"at least {SECRET_KEY_MIN_BYTES} are required. "
                "Generate one with: openssl rand -base64 48"
            )
        return raw

    key_file = settings.secret_key_path
    if key_file.exists():
        raw = key_file.read_bytes().strip()
        if len(raw) < SECRET_KEY_MIN_BYTES:
            raise SecretKeyError(
                f"{key_file} contains a key that is too short. "
                "Delete it to regenerate (this invalidates stored credentials), "
                "or set CAIRN_SECRET_KEY."
            )
        return raw

    generated = secrets.token_urlsafe(48)
    key_file.parent.mkdir(parents=True, exist_ok=True)
    key_file.write_text(generated, encoding="utf-8")
    try:
        key_file.chmod(0o600)
    except OSError:  # pragma: no cover — Windows / exotic filesystems
        log.warning("could not chmod 600 the secret key file", extra={"path": str(key_file)})

    log.warning(
        "=" * 72
        + "\nGENERATED A NEW MASTER KEY\n"
        + f"Written to: {key_file}\n"
        + "Set it as CAIRN_SECRET_KEY in your container config and back it up.\n"
        + "Losing this key makes every stored cookie jar unrecoverable.\n"
        + "=" * 72
    )
    return generated.encode("utf-8")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """Test helper — drop the cached Settings instance."""
    get_settings.cache_clear()
