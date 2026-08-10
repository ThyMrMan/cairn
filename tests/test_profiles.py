"""Cookie jar parsing, coverage checking, and sealed storage (docs/06)."""

from __future__ import annotations

import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from cairn.crypto.sealing import Sealer
from cairn.db.models import AccessProfile
from cairn.db.types import utcnow
from cairn.services import profiles

FUTURE = int((utcnow() + timedelta(days=30)).timestamp())
PAST = int((utcnow() - timedelta(days=30)).timestamp())

JAR = f"""# Netscape HTTP Cookie File
.blogspot.com\tTRUE\t/\tFALSE\t{FUTURE}\tCONSENT\tYES+1
example.blogspot.com\tFALSE\t/\tFALSE\t0\tSESSION_ID\tabc123
"""


def make_profile(session: Session, name: str = "blogger") -> AccessProfile:
    profile = AccessProfile(name=name, mode="cookies", created_at=utcnow(), updated_at=utcnow())
    session.add(profile)
    session.flush()
    return profile


# ── parsing ──────────────────────────────────────────────────────────────


def test_parses_a_normal_jar() -> None:
    report = profiles.parse_cookies(JAR)
    assert report.ok
    assert len(report.cookies) == 2
    assert report.session_cookies == 1
    assert report.hosts_covered == [".blogspot.com", "example.blogspot.com"]


def test_reports_the_offending_line_number() -> None:
    """'It didn't work' is useless; 'line 3 has 4 fields' is fixable."""
    bad = JAR + "example.com\tTRUE\t/\tonly-four-fields\n"
    report = profiles.parse_cookies(bad)
    assert not report.ok
    assert any("Line 4" in e for e in report.errors)


def test_accepts_space_separated_exports() -> None:
    """Some exporters emit spaces; rejecting them helps nobody."""
    report = profiles.parse_cookies(f".blogspot.com TRUE / FALSE {FUTURE} CONSENT YES+1")
    assert report.ok
    assert report.cookies[0].name == "CONSENT"


def test_handles_httponly_prefix_and_crlf() -> None:
    jar = f"#HttpOnly_.blogspot.com\tTRUE\t/\tFALSE\t{FUTURE}\tSID_X\tv\r\n"
    report = profiles.parse_cookies(jar)
    assert report.ok
    assert report.cookies[0].http_only
    assert report.cookies[0].domain == ".blogspot.com"


def test_comments_and_blank_lines_are_skipped() -> None:
    jar = f"# a comment\n\n   \n.blogspot.com\tTRUE\t/\tFALSE\t{FUTURE}\tA\tb\n"
    assert len(profiles.parse_cookies(jar).cookies) == 1


def test_empty_input_is_an_error_not_an_empty_success() -> None:
    report = profiles.parse_cookies("# Netscape HTTP Cookie File\n")
    assert not report.ok
    assert report.errors


def test_warns_when_no_session_cookies_are_present() -> None:
    """The single most common silent failure of the Blogger bypass."""
    jar = f".blogspot.com\tTRUE\t/\tFALSE\t{FUTURE}\tCONSENT\tYES\n"
    report = profiles.parse_cookies(jar)
    assert any("session cookie" in w for w in report.warnings)


def test_flags_expired_cookies() -> None:
    jar = f".blogspot.com\tTRUE\t/\tFALSE\t{PAST}\tOLD\tv\n"
    report = profiles.parse_cookies(jar)
    assert report.expired_cookies == 1
    assert any("expired" in w for w in report.warnings)


def test_flags_a_full_google_account_session() -> None:
    """A jar that can log into someone's Google account is a very different
    asset from one that dismisses a content warning."""
    jar = (
        f".google.com\tTRUE\t/\tTRUE\t{FUTURE}\tSID\tsecret\n"
        f".google.com\tTRUE\t/\tTRUE\t{FUTURE}\t__Secure-1PSID\tsecret\n"
    )
    report = profiles.parse_cookies(jar)
    assert "SID" in report.sensitive
    assert any("account session" in w for w in report.warnings)


def test_earliest_expiry_drives_the_warning() -> None:
    soon = int((utcnow() + timedelta(days=2)).timestamp())
    jar = f".blogspot.com\tTRUE\t/\tFALSE\t{soon}\tC\tv\n"
    report = profiles.parse_cookies(jar)
    assert report.earliest_expiry is not None
    assert report.earliest_expiry > datetime.now(UTC)
    assert any("expires in under" in w for w in report.warnings)


def test_oversized_input_is_rejected_without_parsing() -> None:
    report = profiles.parse_cookies("x" * (profiles.MAX_COOKIE_BYTES + 1))
    assert not report.ok
    assert "1 MB" in report.errors[0]


# ── coverage ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("cookie_domain", "host", "expected"),
    [
        (".blogspot.com", "example.blogspot.com", True),
        (".blogspot.com", "1.bp.blogspot.com", True),
        ("example.blogspot.com", "example.blogspot.com", True),
        ("example.blogspot.com", "other.blogspot.com", False),
        (".google.com", "example.blogspot.com", False),
        # Must not match a host that merely ends with the same characters.
        (".blogspot.com", "notblogspot.com", False),
    ],
)
def test_domain_matching(cookie_domain: str, host: str, expected: bool) -> None:
    assert profiles.domain_matches(cookie_domain, host) is expected


def test_coverage_warns_about_the_seed_host() -> None:
    """The check that converts a six-hour waste into a warning beforehand."""
    report = profiles.parse_cookies(f".google.com\tTRUE\t/\tFALSE\t{FUTURE}\tSID\tv\n")
    covered = profiles.coverage(report, ["example.blogspot.com", "1.bp.blogspot.com"])
    assert covered == {"example.blogspot.com": False, "1.bp.blogspot.com": False}
    warnings = profiles.coverage_warnings(covered, "example.blogspot.com")
    assert any("does not cover example.blogspot.com" in w for w in warnings)


def test_coverage_is_quiet_when_the_seed_host_is_covered() -> None:
    report = profiles.parse_cookies(JAR)
    covered = profiles.coverage(report, ["example.blogspot.com"])
    assert covered["example.blogspot.com"]
    assert profiles.coverage_warnings(covered, "example.blogspot.com") == []


# ── storage ──────────────────────────────────────────────────────────────


def test_store_and_load_roundtrip(db: Session, sealer: Sealer) -> None:
    profile = make_profile(db)
    report = profiles.store_cookies(db, sealer, profile, JAR)

    assert report.ok
    assert profile.cookies_enc is not None
    assert b"SESSION_ID" not in profile.cookies_enc, "material must be sealed at rest"
    assert profile.fingerprint and profile.fingerprint.startswith("sha256:")

    restored = profiles.load_cookies(sealer, profile)
    assert restored is not None
    assert "SESSION_ID" in restored


def test_summary_never_exposes_material(db: Session, sealer: Sealer) -> None:
    profile = make_profile(db)
    profiles.store_cookies(db, sealer, profile, JAR)
    blob = repr(profiles.summary(profile))
    assert "abc123" not in blob
    assert "YES+1" not in blob
    assert "cookies_enc" not in blob


def test_storing_a_broken_jar_raises_and_writes_nothing(db: Session, sealer: Sealer) -> None:
    profile = make_profile(db)
    with pytest.raises(profiles.ProfileError):
        profiles.store_cookies(db, sealer, profile, "garbage without tabs")
    assert profile.cookies_enc is None


def test_fingerprint_changes_when_the_jar_changes(db: Session, sealer: Sealer) -> None:
    profile = make_profile(db)
    profiles.store_cookies(db, sealer, profile, JAR)
    first = profile.fingerprint
    profiles.store_cookies(db, sealer, profile, JAR.replace("abc123", "def456"))
    assert profile.fingerprint != first


def test_clear_material_removes_everything_derived(db: Session, sealer: Sealer) -> None:
    profile = make_profile(db)
    profiles.store_cookies(db, sealer, profile, JAR)
    profiles.clear_material(db, profile)
    assert profile.cookies_enc is None
    assert profile.fingerprint is None
    assert profile.expires_at is None
    assert profile.cookie_meta is None


# ── materialization ──────────────────────────────────────────────────────


def test_materialize_writes_a_readable_jar(db: Session, sealer: Sealer, tmp_path: Path) -> None:
    profile = make_profile(db)
    profile.user_agent = "Mozilla/5.0 (test)"
    profiles.store_cookies(db, sealer, profile, JAR)

    material = profiles.materialize(db, sealer, profile.id, tmp_path / "job-1")
    assert material is not None
    text = material.cookies_file.read_text()
    assert "SESSION_ID" in text
    assert text.endswith("\n")
    assert material.user_agent == "Mozilla/5.0 (test)"


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_materialized_jar_is_not_world_readable(
    db: Session, sealer: Sealer, tmp_path: Path
) -> None:
    """It is a credential on disk for the life of the job."""
    profile = make_profile(db)
    profiles.store_cookies(db, sealer, profile, JAR)
    material = profiles.materialize(db, sealer, profile.id, tmp_path / "job-1")
    assert material is not None
    mode = stat.S_IMODE(material.cookies_file.stat().st_mode)
    assert mode & (stat.S_IRGRP | stat.S_IROTH) == 0


def test_materialize_returns_none_when_nothing_is_stored(
    db: Session, sealer: Sealer, tmp_path: Path
) -> None:
    profile = make_profile(db)
    assert profiles.materialize(db, sealer, profile.id, tmp_path / "job-1") is None
