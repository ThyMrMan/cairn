"""Access profiles: parsing, validating, sealing, and materializing cookies.

M1 implements `cookies` mode only; `userscript` and `interactive` mint a jar
through a headless browser in M5 and reach the engine through exactly this
path (docs/06). The engine only ever sees `--load-cookies`, which is what
makes the per-site mode selector a choice about *how the credential is
obtained* rather than which crawler may be used.

The validation here is the whole point of the feature. A jar that parses but
covers the wrong domain produces a six-hour crawl of interstitial pages with
no error anywhere — so the parse report names the hosts covered, counts the
session cookies, and flags a jar carrying a full Google account session.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from cairn.crypto.sealing import Sealer
from cairn.db.models import AccessProfile
from cairn.db.types import to_iso, utcnow
from cairn.logging import get_logger

log = get_logger(__name__)

COOKIES_CONTEXT = "profile.cookies"
SCRIPT_CONTEXT = "profile.script"
COOKIE_FILE_NAME = "cookies.txt"
MAX_COOKIE_BYTES = 1024 * 1024
NETSCAPE_FIELDS = 7
HTTPONLY_PREFIX = "#HttpOnly_"

# Cookies that carry a full Google account session rather than a per-blog
# consent. A whole-browser export routinely includes these, and a jar that can
# log into someone's account is a very different asset from one that dismisses
# a content warning (docs/06).
SENSITIVE_NAMES = frozenset(
    {"SID", "SSID", "HSID", "APISID", "SAPISID", "LSID", "NID", "__Secure-1PSID",
     "__Secure-3PSID", "__Secure-1PSIDTS", "__Secure-3PSIDTS", "OSID", "ACCOUNT_CHOOSER"}
)  # fmt: skip
_SENSITIVE_PREFIXES = ("__Secure-", "__Host-")


class ProfileError(ValueError):
    """A profile could not be parsed or stored."""


@dataclass(slots=True)
class Cookie:
    domain: str
    include_subdomains: bool
    path: str
    secure: bool
    expires: int
    name: str
    value: str
    http_only: bool = False

    @property
    def is_session(self) -> bool:
        return self.expires == 0

    def to_line(self) -> str:
        prefix = HTTPONLY_PREFIX if self.http_only else ""
        return "\t".join(
            (
                f"{prefix}{self.domain}",
                "TRUE" if self.include_subdomains else "FALSE",
                self.path or "/",
                "TRUE" if self.secure else "FALSE",
                str(self.expires),
                self.name,
                self.value,
            )
        )


@dataclass(slots=True)
class CookieReport:
    """What the UI shows after an upload, before anything is saved."""

    cookies: list[Cookie] = field(default_factory=list)
    hosts_covered: list[str] = field(default_factory=list)
    session_cookies: int = 0
    expired_cookies: int = 0
    earliest_expiry: datetime | None = None
    sensitive: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors and bool(self.cookies)

    def to_dict(self) -> dict[str, Any]:
        return {
            "cookie_count": len(self.cookies),
            "hosts_covered": self.hosts_covered,
            "session_cookies": self.session_cookies,
            "expired_cookies": self.expired_cookies,
            "earliest_expiry": to_iso(self.earliest_expiry) if self.earliest_expiry else None,
            "sensitive": self.sensitive,
            "warnings": self.warnings,
            "errors": self.errors,
            "ok": self.ok,
        }


def parse_cookies(text: str) -> CookieReport:
    """Parse a Netscape `cookies.txt`, reporting problems by line number.

    Tolerant where tolerance is safe (blank lines, comments, CRLF, a missing
    header) and strict where it is not — a line with the wrong field count is
    an error naming the line, because the alternative is a jar that silently
    lost the one cookie that mattered.
    """
    report = CookieReport()
    if len(text.encode("utf-8", errors="ignore")) > MAX_COOKIE_BYTES:
        report.errors.append("The file is larger than 1 MB; that is not a cookie jar.")
        return report

    now = utcnow()
    expiries: list[int] = []

    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.rstrip("\r")
        if not line.strip():
            continue

        http_only = line.startswith(HTTPONLY_PREFIX)
        if http_only:
            line = line[len(HTTPONLY_PREFIX) :]
        elif line.lstrip().startswith("#"):
            continue

        fields = line.split("\t")
        if len(fields) != NETSCAPE_FIELDS:
            # Some exporters use spaces. Retry before rejecting the line.
            fields = re.split(r"\s+", line.strip(), maxsplit=NETSCAPE_FIELDS - 1)
        if len(fields) != NETSCAPE_FIELDS:
            report.errors.append(
                f"Line {number}: expected {NETSCAPE_FIELDS} tab-separated fields, "
                f"found {len(fields)}."
            )
            continue

        domain, include_sub, path, secure, expires_raw, name, value = fields
        try:
            expires = int(float(expires_raw))
        except ValueError:
            report.errors.append(f"Line {number}: {expires_raw!r} is not an expiry timestamp.")
            continue
        if not domain:
            report.errors.append(f"Line {number}: no domain.")
            continue
        if not name:
            report.errors.append(f"Line {number}: no cookie name.")
            continue

        cookie = Cookie(
            domain=domain.lower(),
            include_subdomains=include_sub.strip().upper() == "TRUE",
            path=path or "/",
            secure=secure.strip().upper() == "TRUE",
            expires=max(expires, 0),
            name=name,
            value=value,
            http_only=http_only,
        )
        report.cookies.append(cookie)

        if cookie.is_session:
            report.session_cookies += 1
        else:
            if cookie.expires <= now.timestamp():
                report.expired_cookies += 1
            expiries.append(cookie.expires)

        if cookie.name in SENSITIVE_NAMES or cookie.name.startswith(_SENSITIVE_PREFIXES):
            report.sensitive.append(cookie.name)

    if not report.cookies and not report.errors:
        report.errors.append("No cookies found. Is this a Netscape-format cookies.txt?")

    report.hosts_covered = sorted({c.domain for c in report.cookies})
    if expiries:
        from datetime import UTC

        report.earliest_expiry = datetime.fromtimestamp(min(expiries), tz=UTC)

    _add_warnings(report, now)
    return report


def _add_warnings(report: CookieReport, now: datetime) -> None:
    if report.cookies and report.session_cookies == 0:
        report.warnings.append(
            "No session cookies found. Blogger's bypass often uses one, and many "
            "exporters drop them — re-export with 'include session cookies' enabled."
        )
    if report.expired_cookies:
        report.warnings.append(
            f"{report.expired_cookies} cookie(s) have already expired and will be ignored."
        )
    if report.sensitive:
        names = ", ".join(sorted(set(report.sensitive))[:6])
        report.warnings.append(
            f"This jar includes full account session cookies ({names}). Only the "
            "interstitial cookie is needed — consider exporting a narrower set."
        )
    if report.earliest_expiry is not None:
        days = (report.earliest_expiry - now).total_seconds() / 86400
        if 0 < days < 7:
            report.warnings.append(
                f"The earliest cookie expires in under {int(days) + 1} day(s); "
                "long captures may lose access partway through."
            )


# ── coverage ─────────────────────────────────────────────────────────────


def domain_matches(cookie_domain: str, host: str) -> bool:
    """Whether a jar entry would be sent to `host`.

    A leading dot means the domain and all subdomains — which is exactly the
    distinction that decides whether one Blogger profile covers every blogspot
    site or only the blog it was exported from.
    """
    cookie_domain = cookie_domain.lower().lstrip(".")
    host = host.lower()
    return host == cookie_domain or host.endswith(f".{cookie_domain}")


def coverage(report: CookieReport, hosts: list[str]) -> dict[str, bool]:
    """Which of a site's scope hosts the jar actually covers."""
    return {host: any(domain_matches(c.domain, host) for c in report.cookies) for host in hosts}


def coverage_warnings(covered: dict[str, bool], seed_host: str) -> list[str]:
    missing = [h for h, ok in covered.items() if not ok]
    warnings: list[str] = []
    if not covered.get(seed_host, False):
        warnings.append(
            f"This profile does not cover {seed_host}, so its cookies will not be "
            "sent and the capture will see whatever an anonymous visitor sees."
        )
    other = [h for h in missing if h != seed_host]
    if other:
        warnings.append(
            f"Not covered: {', '.join(other[:5])}"
            f"{'…' if len(other) > 5 else ''}. Assets from those hosts may fail."
        )
    return warnings


# ── storage ──────────────────────────────────────────────────────────────


def fingerprint(text: str) -> str:
    """Identity of the stored material, so the UI can show that it changed.

    A hash, never the material — this value is returned by the API.
    """
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def store_cookies(
    session: Session, sealer: Sealer, profile: AccessProfile, text: str
) -> CookieReport:
    report = parse_cookies(text)
    if not report.ok:
        raise ProfileError(report.errors[0] if report.errors else "no usable cookies")

    canonical = "# Netscape HTTP Cookie File\n" + "\n".join(c.to_line() for c in report.cookies)
    profile.cookies_enc = sealer.seal(canonical, context=COOKIES_CONTEXT)
    profile.mode = "cookies"
    profile.hosts = report.hosts_covered
    profile.expires_at = report.earliest_expiry
    profile.fingerprint = fingerprint(canonical)
    profile.minted_at = utcnow()
    profile.updated_at = utcnow()
    # Non-secret summary, so the UI can render the report without unsealing.
    profile.cookie_meta = {
        "cookie_count": len(report.cookies),
        "session_cookies": report.session_cookies,
        "hosts_covered": report.hosts_covered,
        "sensitive": sorted(set(report.sensitive)),
        "warnings": report.warnings,
    }
    session.flush()
    log.info(
        "profile material stored",
        extra={"profile": profile.id, "cookies": len(report.cookies)},
    )
    return report


def clear_material(session: Session, profile: AccessProfile) -> None:
    profile.cookies_enc = None
    profile.script_enc = None
    profile.cookie_meta = None
    profile.fingerprint = None
    profile.expires_at = None
    profile.minted_at = None
    profile.updated_at = utcnow()
    session.flush()


def load_cookies(sealer: Sealer, profile: AccessProfile) -> str | None:
    if profile.cookies_enc is None:
        return None
    return sealer.unseal_text(profile.cookies_enc, context=COOKIES_CONTEXT)


@dataclass(slots=True)
class Material:
    cookies_file: Path
    user_agent: str | None


def materialize(
    session: Session, sealer: Sealer, profile_id: int, temp_dir: Path
) -> Material | None:
    """Write the plaintext jar into a job's temp directory.

    This is the only place the material exists unencrypted, it never touches
    the archive tree, and the supervisor deletes the directory when the job
    ends — with a boot sweep of /data/tmp closing the crash window (docs/06).
    """
    profile = session.get(AccessProfile, profile_id)
    if profile is None:
        return None
    text = load_cookies(sealer, profile)
    if text is None:
        log.warning("site has a profile with no cookies stored", extra={"profile": profile_id})
        return None

    temp_dir.mkdir(parents=True, exist_ok=True)
    target = temp_dir / COOKIE_FILE_NAME
    # Written 600 before any content lands in it: creating world-readable and
    # narrowing afterwards leaves a window where any process could read it.
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text if text.endswith("\n") else text + "\n")

    return Material(cookies_file=target, user_agent=profile.user_agent)


def summary(profile: AccessProfile) -> dict[str, Any]:
    """The API shape — metadata only, never material (docs/06)."""
    meta: dict[str, Any] = profile.cookie_meta or {}
    return {
        "id": profile.id,
        "name": profile.name,
        "mode": profile.mode,
        "hosts": profile.hosts or [],
        "user_agent": profile.user_agent,
        "cookie_count": meta.get("cookie_count", 0),
        "session_cookie_count": meta.get("session_cookies", 0),
        "hosts_covered": meta.get("hosts_covered", []),
        "sensitive": meta.get("sensitive", []),
        "warnings": meta.get("warnings", []),
        "has_material": profile.cookies_enc is not None or profile.script_enc is not None,
        "minted_at": profile.minted_at,
        "expires_at": profile.expires_at,
        "fingerprint": profile.fingerprint,
        "last_verified_at": profile.last_verified_at,
        "last_verify_result": profile.last_verify_result,
        "verify_url": profile.verify_url,
        "notes": profile.notes,
        "created_at": profile.created_at,
    }
