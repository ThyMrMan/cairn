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
import io
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
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
STORAGE_CONTEXT = "profile.storage"
BROWSER_PROFILE_CONTEXT = "profile.browser"
COOKIE_FILE_NAME = "cookies.txt"
BROWSER_PROFILE_FILE_NAME = "profile.tar.gz"
MAX_COOKIE_BYTES = 1024 * 1024

# A browsertrix profile tarball. Measured at 41 MB for one Google login, so
# the ceiling is generous — and it is the reason this one material does not
# live in a database column like the others: `list_profiles` would drag every
# byte of it into memory on each page load.
MAX_BROWSER_PROFILE_BYTES = 512 * 1024 * 1024
# How many host names a profile readout lists. The count beside it is the
# real total, so this only bounds the chips, not the truth.
HOST_LIST_LIMIT = 200
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
    session: Session,
    sealer: Sealer,
    profile: AccessProfile,
    text: str,
    *,
    mode: str = "cookies",
) -> CookieReport:
    """Seal a jar onto a profile, whatever produced it.

    `mode` says where it came from — an upload, a mint, or an interactive
    session — and it matters beyond bookkeeping: only a `userscript` profile
    can be re-minted automatically, because only it still has the script that
    would do the minting.
    """
    report = parse_cookies(text)
    if not report.ok:
        raise ProfileError(report.errors[0] if report.errors else "no usable cookies")

    canonical = "# Netscape HTTP Cookie File\n" + "\n".join(c.to_line() for c in report.cookies)
    profile.cookies_enc = sealer.seal(canonical, context=COOKIES_CONTEXT)
    profile.mode = mode
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


def store_script(session: Session, sealer: Sealer, profile: AccessProfile, text: str) -> Any:
    """Seal a userscript onto a profile and return what was parsed out of it.

    Sealed like the cookies, for a reason worth stating: a userscript that
    dismisses an interstitial usually encodes something about how to get past
    that site, and the archive tree it would otherwise sit in is shared over
    SMB and copied into backups.
    """
    from cairn.services import userscripts

    script = userscripts.parse(text)
    profile.script_enc = sealer.seal(text, context=SCRIPT_CONTEXT)
    profile.mode = "userscript"
    profile.updated_at = utcnow()
    session.flush()
    log.info("userscript stored", extra={"profile": profile.id, "name": script.name})
    return script


def load_script(sealer: Sealer, profile: AccessProfile) -> str | None:
    if profile.script_enc is None:
        return None
    return sealer.unseal_text(profile.script_enc, context=SCRIPT_CONTEXT)


def store_storage_state(
    session: Session, sealer: Sealer, profile: AccessProfile, state: dict[str, Any]
) -> None:
    """Keep the full browser state a session ended with.

    A login that keeps its token in localStorage cannot be rebuilt from
    cookies alone, so it is captured while the browser still has it. What
    reads it back is every browser path in the system — the re-mint, the
    verification, browser-based discovery — and what does not is wget, which
    takes `--load-cookies` and nothing else.

    docs/13 hoped this would also make a profile work with
    `browsertrix-crawler --profile`. It does not, and M7 measured why:
    browsertrix runs **Brave** while this image ships Chrome for Testing, and
    a profile tarball built with one is accepted and ignored by the other. The
    value here is the browser paths inside this application, not a bridge to
    that one.
    """
    import json

    profile.storage_enc = sealer.seal(json.dumps(state), context=STORAGE_CONTEXT)
    profile.cookie_meta = {**(profile.cookie_meta or {}), "storage": describe_storage(state)}
    profile.updated_at = utcnow()
    session.flush()


def load_storage_state(sealer: Sealer, profile: AccessProfile) -> dict[str, Any] | None:
    """The saved browser state, or None. Never leaves the process."""
    if profile.storage_enc is None:
        return None
    import json

    try:
        loaded = json.loads(sealer.unseal_text(profile.storage_enc, context=STORAGE_CONTEXT))
    except (ValueError, TypeError):  # pragma: no cover — corrupted ciphertext
        log.warning("stored browser state could not be read", extra={"profile": profile.id})
        return None
    return loaded if isinstance(loaded, dict) else None


def describe_storage(state: dict[str, Any]) -> dict[str, Any]:
    """What is in a storage state, without any of what is in it.

    Metadata only, like everything else a profile exposes — counts and origins,
    never a key and never a value. The count that matters is `local_items`: a
    profile with plenty of those and few cookies is a login wget cannot use,
    and nothing else in the system would say so.
    """
    origins = state.get("origins") or []
    items = 0
    hosts: list[str] = []
    for origin in origins if isinstance(origins, list) else []:
        if not isinstance(origin, dict):
            continue
        name = str(origin.get("origin") or "")
        if name:
            hosts.append(name)
        entries = origin.get("localStorage") or []
        items += len(entries) if isinstance(entries, list) else 0
    cookies = state.get("cookies") or []
    return {
        "cookies": len(cookies) if isinstance(cookies, list) else 0,
        "origins": sorted(set(hosts))[:20],
        "local_items": items,
    }


def storage_note(meta: dict[str, Any] | None) -> str | None:
    """The sentence to show when a profile holds more than wget can use."""
    storage = (meta or {}).get("storage") or {}
    items = int(storage.get("local_items") or 0)
    if items <= 0:
        return None
    return (
        f"This profile also holds {items} localStorage item(s) from "
        f"{len(storage.get('origins') or [])} origin(s). The browser engines and the "
        "profile test use them; the wget engine cannot — it is handed cookies and "
        "nothing else. If a capture with this profile still gets the sign-in page while "
        "the test passes, that is why."
    )


# ── browsertrix browser profiles ─────────────────────────────────────────
#
# The one credential this application does not mint. browsertrix runs Brave
# and cannot take a cookie jar, so a profile built by our own Chromium is
# accepted and silently ignored (docs/06) — which left gated sites with no
# browser engine at all, and therefore no way to archive a site whose content
# is built by script *and* sits behind a login.
#
# What closes it is browsertrix's own `create-login-profile`, running the same
# browser in the same image, headful under Xvfb. That last part is why it gets
# through sign-ins our CDP screencast cannot: the headless fingerprint is
# simply absent. Cairn does not run it — it takes the tarball it produces.
#
# Stored on disk rather than in a column because it is two orders of magnitude
# larger than every other material here, and sealed all the same: it is a live
# browser session, which is a stronger credential than the cookie jar.


# Chromium counts from 1601-01-01, in microseconds. The offset to the Unix
# epoch, which is also what browsertrix's own `--cookieDays` converts through.
CHROMIUM_EPOCH_OFFSET_S = 11_644_473_600


def _chromium_epoch(expires_utc: Any) -> int | None:
    """One Chromium `expires_utc` as a Unix timestamp, or None if it is not one.

    Zero means a session cookie — no expiry rather than an expiry in 1601 —
    and is handled by the caller, which counts those separately. Anything
    outside a plausible range is discarded rather than reported: a corrupt row
    that reads as the year 30828 would otherwise become "expires in 28,000
    years" on the profile card.
    """
    try:
        seconds = int(expires_utc) // 1_000_000 - CHROMIUM_EPOCH_OFFSET_S
    except (TypeError, ValueError):
        return None
    # 2001-09-09 to 2100. Below the floor is a clock that was never set.
    return seconds if 1_000_000_000 < seconds < 4_102_444_800 else None


def describe_browser_profile(raw: bytes) -> dict[str, Any]:
    """Which hosts a browsertrix profile tarball actually carries cookies for.

    The question somebody has after uploading one is "did that work, and does
    it cover this blog?", and size and a digest cannot answer either. A tarball
    is a Brave user-data-dir, so the answer is in `Default/Cookies` — a SQLite
    database whose `host_key` and `name` are plaintext while only the values
    are encrypted, which is exactly the half worth reading.

    **No `Default/Cookies` member at all means the session stored no cookies**,
    which is the signal that the profile browser never got past the gate or was
    committed before clicking through. That is worth reporting as a finding
    rather than as an empty list.

    Values are never read, never stored and never returned, the same rule the
    cookie jar and the storage state follow — and with more force here, because
    a browser profile is a live session.
    """
    import sqlite3
    import tarfile
    import tempfile

    report: dict[str, Any] = {
        "hosts": [],
        "host_count": 0,
        "cookies": 0,
        "session_cookies": 0,
        "readable": False,
    }
    wanted = ("Default/Cookies", "./Default/Cookies")
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
            # `readable` means the tarball opened, not that a cookie store was
            # found in it — the two answer different questions and the UI needs
            # both: "this is not a tarball" and "this is a tarball holding no
            # cookies" have completely different fixes.
            report["readable"] = True
            member = next((m for m in archive.getmembers() if m.name in wanted), None)
            if member is None:
                return report
            handle = archive.extractfile(member)
            if handle is None:  # pragma: no cover — a directory named Cookies
                return report
            blob = handle.read()
    except (tarfile.TarError, OSError, EOFError):
        return report

    # sqlite3 needs a path, so the database is copied out — never
    # `archive.extract()`, which would let a member name choose where it lands.
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "Cookies"
        copy.write_bytes(blob)
        try:
            connection = sqlite3.connect(f"file:{copy}?mode=ro", uri=True)
            try:
                rows = connection.execute("SELECT host_key, expires_utc FROM cookies").fetchall()
            finally:
                connection.close()
        except sqlite3.Error:
            return report

    hosts: dict[str, int] = {}
    soonest: dict[str, int] = {}
    session = 0
    for host_key, expires in rows:
        host = str(host_key or "")
        if host:
            hosts[host] = hosts.get(host, 0) + 1
        if not expires:
            session += 1
            continue
        stamp = _chromium_epoch(expires)
        if stamp is not None and host:
            known = soonest.get(host)
            if known is None or stamp < known:
                soonest[host] = stamp

    report["cookies"] = len(rows)
    report["session_cookies"] = session
    # Per host rather than one number for the profile, and that is the whole
    # design of this part. A tarball holds consent and analytics cookies that
    # expire next week alongside the sign-in that lasts months, so "the
    # earliest cookie in the file" is a date that means nothing and would cry
    # wolf every time. Which host's cookies matter is a question only the
    # *site* can answer — it knows what it is crawling — so the readout says
    # what it saw per host and the answering is done where the context is.
    report["expiries"] = {
        host: to_iso(datetime.fromtimestamp(ts, tz=UTC)) for host, ts in soonest.items()
    }
    # The true count, separate from the list. Deriving "how many hosts?" from
    # a truncated list makes the cap the answer: a profile covering 47 hosts
    # reported 30, and would have reported 30 for any larger number too.
    report["host_count"] = len(hosts)
    # Busiest first: the host with the most cookies is the login, and a
    # profile that browsed for a while has plenty. Still capped, because this
    # is stored as JSON on the profile row rather than in a table, but high
    # enough that the cap is now an edge rather than the common case.
    report["hosts"] = [h for h, _ in sorted(hosts.items(), key=lambda kv: (-kv[1], kv[0]))][
        :HOST_LIST_LIMIT
    ]
    return report


def browser_profile_path(settings: Any, profile_id: int) -> Path:
    """Where a sealed tarball lives. `personas_dir` was created at boot and
    never used — the personas work ended up in `storage_enc` instead."""
    return Path(settings.personas_dir) / f"{profile_id}.tar.gz.enc"


def store_browser_profile(
    session: Session, sealer: Sealer, profile: AccessProfile, settings: Any, source: Path
) -> dict[str, Any]:
    """Seal a browsertrix profile tarball and record what it is.

    The digest is over the *plaintext*, so it identifies the tarball rather
    than this particular encryption of it — two seals of one file differ byte
    for byte, and "did the profile change?" is the question worth answering.
    """
    raw = source.read_bytes()
    if not raw:
        raise ProfileError("That file is empty.")
    if len(raw) > MAX_BROWSER_PROFILE_BYTES:
        raise ProfileError(
            f"That file is larger than {MAX_BROWSER_PROFILE_BYTES // (1024 * 1024)} MB."
        )
    # Cheap sanity check with a real payoff: uploading the *log* instead of the
    # tarball would otherwise be discovered as a crawl that archived the login
    # page several thousand times.
    if not raw.startswith(b"\x1f\x8b"):
        raise ProfileError(
            "That is not a gzip file. Upload the profile.tar.gz that "
            "create-login-profile wrote, not its log or a folder."
        )

    target = browser_profile_path(settings, profile.id)
    target.parent.mkdir(parents=True, exist_ok=True)
    sealed = sealer.seal(raw, context=BROWSER_PROFILE_CONTEXT)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(sealed)

    meta = {
        "size": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest()[:16],
        "stored_at": to_iso(utcnow()),
        # Read here rather than on every list: the tarball is tens of
        # megabytes and `GET /api/profiles` would have to unseal all of it.
        **describe_browser_profile(raw),
    }
    profile.cookie_meta = {**(profile.cookie_meta or {}), "browser_profile": meta}
    profile.updated_at = utcnow()
    session.flush()
    log.info("browser profile stored", extra={"profile": profile.id, "bytes": len(raw)})
    return meta


def has_browser_profile(profile: AccessProfile) -> bool:
    return bool((profile.cookie_meta or {}).get("browser_profile"))


def write_browser_profile(
    sealer: Sealer, profile: AccessProfile, settings: Any, target: Path
) -> Path | None:
    """Unseal the tarball into a job's temp directory, or None if there isn't one.

    Streams nothing: it is one read and one write of a few tens of megabytes,
    and a chunked frame format would be a second thing to get wrong for no
    benefit at this size.
    """
    if not has_browser_profile(profile):
        return None
    source = browser_profile_path(settings, profile.id)
    if not source.is_file():
        log.warning(
            "profile claims a browser profile that is not on disk",
            extra={"profile": profile.id, "path": str(source)},
        )
        return None

    target.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(sealer.unseal(source.read_bytes(), context=BROWSER_PROFILE_CONTEXT))
    return target


def clear_browser_profile(session: Session, profile: AccessProfile, settings: Any) -> None:
    meta = dict(profile.cookie_meta or {})
    meta.pop("browser_profile", None)
    profile.cookie_meta = meta or None
    profile.updated_at = utcnow()
    browser_profile_path(settings, profile.id).unlink(missing_ok=True)
    session.flush()


def clear_material(session: Session, profile: AccessProfile, settings: Any = None) -> None:
    # `settings` is optional only so the service tests can call this without
    # one. Pass it from anything with a filesystem: dropping `cookie_meta`
    # forgets that a browser profile exists, and without this the sealed
    # tarball stays on disk with nothing left pointing at it.
    if settings is not None:
        browser_profile_path(settings, profile.id).unlink(missing_ok=True)
    profile.cookies_enc = None
    profile.script_enc = None
    profile.storage_enc = None
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
    # Optional since a browsertrix profile is a complete credential on its own
    # and comes with no jar — that engine has no cookie option to hand one to.
    cookies_file: Path | None
    user_agent: str | None
    # The full browser state, for the callers that can use one. Held in memory
    # rather than written beside the jar: nothing outside this process reads
    # it, and a second plaintext credential on disk is a second thing to leak.
    storage_state: dict[str, Any] | None = None
    # The unsealed browsertrix tarball, for the engine that takes `--profile`.
    profile_file: Path | None = None


# Chromium's Linux cookie encryption, for the case where there is no OS
# keyring to hold the real key — which is every container, including the one
# browsertrix runs its Brave in. `OSCrypt` then derives from a **hardcoded**
# password, so the values are readable by anyone holding the file.
#
# Measured against browsertrix-crawler 1.14.1 rather than recalled
# (`scripts/probes/cookie_bridge_probe.py`): a profile it wrote stored a known
# cookie as `v10`, and these parameters returned the exact plaintext.
_CHROMIUM_PASSWORD = b"peanuts"
_CHROMIUM_SALT = b"saltysalt"
_CHROMIUM_ITERATIONS = 1
_CHROMIUM_KEY_BYTES = 16
_CHROMIUM_IV = b" " * 16
_CHROMIUM_PREFIXES = (b"v10", b"v11")
#: Chromium 130+ prepends a SHA-256 of the cookie's domain to the plaintext
#: before encrypting, as a binding check. Present in 1.14.1's Brave.
_DOMAIN_HASH_BYTES = 32


def _decrypt_chromium_value(blob: bytes, host: str) -> str | None:
    """One `encrypted_value`, or None if it is not one this can read.

    Returns None rather than guessing. A wrong key decrypts to bytes just the
    same, and a jar full of garbage would fail as a mystery at crawl time
    instead of as a refusal here.
    """
    if not blob:
        return ""
    if blob[:3] not in _CHROMIUM_PREFIXES:
        # `v20` is the app-bound scheme, and there is no unattended way to
        # read it. Saying so is better than shipping half a jar.
        return None
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        key = hashlib.pbkdf2_hmac(
            "sha1",
            _CHROMIUM_PASSWORD,
            _CHROMIUM_SALT,
            _CHROMIUM_ITERATIONS,
            dklen=_CHROMIUM_KEY_BYTES,
        )
        decryptor = Cipher(algorithms.AES(key), modes.CBC(_CHROMIUM_IV)).decryptor()
        plain = decryptor.update(blob[3:]) + decryptor.finalize()
    except Exception:
        return None

    if plain and 1 <= plain[-1] <= 16:  # PKCS#7
        plain = plain[: -plain[-1]]
    if len(plain) >= _DOMAIN_HASH_BYTES:
        expected = hashlib.sha256(host.lstrip(".").encode("utf-8")).digest()
        if plain[:_DOMAIN_HASH_BYTES] == expected:
            plain = plain[_DOMAIN_HASH_BYTES:]
    try:
        return plain.decode("utf-8")
    except UnicodeDecodeError:
        return None


def cookies_from_browser_profile(raw: bytes, hosts: list[str] | None = None) -> list[Cookie]:
    """A cookie jar out of a browsertrix browser profile.

    The bridge D4 assumed existed. `docs/00` D4 says every auth mode ends as a
    jar and the engine only ever sees `--load-cookies`; that stopped being true
    when the browser profile arrived, because it was the one producer with no
    jar. So a browser profile silently meant "and therefore browsertrix", which
    coupled two choices that have nothing to do with each other.

    **This reads cookie values, which `describe_browser_profile` deliberately
    does not.** That function answers "does this cover my blog?" for the UI and
    must never handle the credential to do it. This one exists to hand the
    credential to an engine, which is the same job `load_cookies` already does
    for a jar-mode profile, under the same terms: written only into the job's
    temp directory, `chmod 600`, deleted when the job ends. The two are kept
    apart by name so neither drifts into the other's guarantees.

    `hosts` narrows the result to the site's own scope, and passing it is
    strongly preferred. A profile minted by signing into Google is a full
    account session; a jar is a far more portable form of that than an
    encrypted 41 MB tarball, and there is no reason a blog crawl needs the
    whole browser's cookies on disk to fetch one host.
    """
    import sqlite3
    import tarfile
    import tempfile

    wanted = ("Default/Cookies", "./Default/Cookies")
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
            member = next((m for m in archive.getmembers() if m.name in wanted), None)
            if member is None:
                return []
            handle = archive.extractfile(member)
            if handle is None:
                return []
            blob = handle.read()
    except (tarfile.TarError, OSError, EOFError):
        return []

    # Copied out rather than extracted, for the same reason as the readout: a
    # member name must never choose where a file lands.
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "Cookies"
        copy.write_bytes(blob)
        try:
            connection = sqlite3.connect(f"file:{copy}?mode=ro", uri=True)
            try:
                rows = connection.execute(
                    "SELECT host_key, name, encrypted_value, path, expires_utc, "
                    "is_secure, is_httponly FROM cookies"
                ).fetchall()
            finally:
                connection.close()
        except sqlite3.Error:
            return []

    patterns = [h.lower().lstrip(".") for h in (hosts or []) if h]
    out: list[Cookie] = []
    for host_key, name, encrypted, path, expires, secure, http_only in rows:
        host = str(host_key or "")
        if not host or not name:
            continue
        if patterns and not any(_host_matches(host, p) for p in patterns):
            continue
        value = _decrypt_chromium_value(bytes(encrypted or b""), host)
        if value is None:
            continue
        stamp = _chromium_epoch(expires)
        out.append(
            Cookie(
                domain=host,
                # Chromium's leading dot is exactly the Netscape flag.
                include_subdomains=host.startswith("."),
                path=str(path or "/"),
                secure=bool(secure),
                # A session cookie is 0 in both formats, so this needs no
                # special case — but wget drops them unless it is told not to,
                # which is why the engine passes --keep-session-cookies.
                expires=stamp if stamp is not None else 0,
                name=str(name),
                value=value,
                http_only=bool(http_only),
            )
        )
    return out


def _host_matches(host_key: str, pattern: str) -> bool:
    """Whether a Chromium `host_key` belongs to a host the site may fetch.

    A leading dot on either side means "and subdomains", which is the whole
    point of the field, so `.blogspot.com` covers `example.blogspot.com` and a
    scope naming `*.blogspot.com` covers it too.
    """
    host = host_key.lower().lstrip(".")
    pattern = pattern.lstrip("*.")
    return host == pattern or host.endswith("." + pattern) or pattern.endswith("." + host)


def jar_from_browser_profile(raw: bytes, hosts: list[str] | None = None) -> str | None:
    """The same cookies as Netscape text, or None if there are none to write."""
    cookies = cookies_from_browser_profile(raw, hosts)
    if not cookies:
        return None
    return "# Netscape HTTP Cookie File\n" + "\n".join(c.to_line() for c in cookies) + "\n"


def materialize(
    session: Session,
    sealer: Sealer,
    profile_id: int,
    temp_dir: Path,
    settings: Any = None,
    hosts: list[str] | None = None,
) -> Material | None:
    """Write the plaintext material into a job's temp directory.

    This is the only place it exists unencrypted, it never touches the archive
    tree, and the supervisor deletes the directory when the job ends — with a
    boot sweep of /data/tmp closing the crash window (docs/06).

    Returns None only when there is *nothing* to hand over. A profile holding
    a browsertrix tarball and no jar is a complete credential for the engine
    that can use one, and refusing it here would be the same silent failure
    this whole feature exists to remove.

    `hosts` is the site's own scope, and it narrows a jar derived from a
    browser profile to the hosts this crawl may fetch. Omitting it writes the
    browser's whole cookie store — every site it has ever visited — into the
    temp directory to fetch one blog, so callers with a site in hand pass it.
    """
    profile = session.get(AccessProfile, profile_id)
    if profile is None:
        return None

    temp_dir.mkdir(parents=True, exist_ok=True)
    text = load_cookies(sealer, profile)

    tarball: Path | None = None
    if settings is not None:
        tarball = write_browser_profile(
            sealer, profile, settings, temp_dir / BROWSER_PROFILE_FILE_NAME
        )

    # A browser profile can now answer for a jar as well as for itself, so a
    # profile minted by signing in works on every engine rather than only on
    # the one that reads tarballs. Derived, never stored: the tarball stays the
    # single source, and a jar that outlived one job would be a credential
    # nobody remembered leaving behind.
    #
    # An uploaded jar still wins. It is what somebody chose for this site, it
    # may be narrower than the browser's whole cookie store, and silently
    # preferring a derivation over a deliberate act is how a profile stops
    # meaning what it says.
    if text is None and tarball is not None:
        try:
            text = jar_from_browser_profile(tarball.read_bytes(), hosts or None)
        except Exception as exc:  # pragma: no cover — unreadable temp file
            log.warning(
                "could not derive a cookie jar from the browser profile",
                extra={"profile": profile_id, "err": str(exc)},
            )
        if text:
            log.info(
                "derived a cookie jar from the browser profile",
                extra={"profile": profile_id, "hosts": len(hosts or [])},
            )

    target: Path | None = None
    if text is not None:
        target = temp_dir / COOKIE_FILE_NAME
        # Written 600 before any content lands in it: creating world-readable
        # and narrowing afterwards leaves a window where anything could read it.
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text if text.endswith("\n") else text + "\n")

    if target is None and tarball is None:
        log.warning("site has a profile with nothing stored in it", extra={"profile": profile_id})
        return None

    return Material(
        cookies_file=target,
        user_agent=profile.user_agent,
        storage_state=load_storage_state(sealer, profile),
        profile_file=tarball,
    )


async def verify(profile: AccessProfile, cookies_text: str) -> dict[str, Any]:
    """Fetch `verify_url` with this jar and report what came back.

    Plain HTTP on purpose. The question is whether *wget* will get real
    content, and a browser would answer a different one — it runs the site's
    JavaScript and can talk its way past a gate that wget then cannot, which
    is the exact failure this check exists to catch.
    """
    import httpx

    from cairn.services import interstitial

    jar = httpx.Cookies()
    for cookie in parse_cookies(cookies_text).cookies:
        jar.set(cookie.name, cookie.value, domain=cookie.domain, path=cookie.path)

    headers = {"User-Agent": profile.user_agent} if profile.user_agent else {}
    try:
        async with httpx.AsyncClient(
            cookies=jar, headers=headers, follow_redirects=True, timeout=20.0
        ) as client:
            response = await client.get(str(profile.verify_url))
    except httpx.HTTPError as exc:
        return {"ok": False, "reason": f"could not reach {profile.verify_url}: {exc}", "status": 0}

    verdict = interstitial.looks_blocked(response.content[: 512 * 1024], str(response.url))
    ok = response.status_code < 400 and verdict.ok
    if response.status_code >= 400:
        reason = f"the site answered {response.status_code}"
    elif verdict.blocked:
        reason = f"still the interstitial — {verdict.reason}"
    else:
        reason = f"real content from {response.url}"
    return {
        "ok": ok,
        "reason": reason,
        "status": response.status_code,
        "final_url": str(response.url),
        "bytes": len(response.content),
    }


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
        "has_material": (
            profile.cookies_enc is not None
            or profile.script_enc is not None
            or has_browser_profile(profile)
        ),
        "has_cookies": profile.cookies_enc is not None,
        "has_script": profile.script_enc is not None,
        "has_storage": profile.storage_enc is not None,
        "has_browser_profile": has_browser_profile(profile),
        # Size and digest only. A tarball is a live browser session, so the
        # same rule applies as everywhere else here: never the material.
        "browser_profile": meta.get("browser_profile"),
        "storage": meta.get("storage") or {},
        "storage_note": storage_note(meta),
        "script": meta.get("script"),
        "minted_at": profile.minted_at,
        "expires_at": profile.expires_at,
        "fingerprint": profile.fingerprint,
        "last_verified_at": profile.last_verified_at,
        "last_verify_result": profile.last_verify_result,
        "verify_url": profile.verify_url,
        "notes": profile.notes,
        "created_at": profile.created_at,
    }
