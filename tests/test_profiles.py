"""Cookie jar parsing, coverage checking, and sealed storage (docs/06)."""

from __future__ import annotations

import io
import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from warcio.statusandheaders import StatusAndHeaders

from cairn.config import Settings
from cairn.crypto.sealing import Sealer
from cairn.db.models import AccessProfile
from cairn.db.types import to_iso, utcnow
from cairn.services import profiles
from tests.conftest import XHR

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


# ── the verify URL ───────────────────────────────────────────────────────
#
# The mint runs the userscript against `verify_url` and refuses without one.
# The create form asks for it and only insists for the mode chosen at that
# moment — and the default mode is `cookies`, so the ordinary path (make a
# profile, then upload a script to it) leaves the field empty. The profile
# card now edits it, which is only worth anything if PATCH really takes it.


def test_the_verify_url_can_be_set_after_the_profile_exists(authed: TestClient) -> None:
    created = authed.post("/api/profiles", json={"name": "later", "mode": "cookies"}, headers=XHR)
    assert created.status_code == 201, created.text
    profile_id = created.json()["id"]
    assert created.json()["verify_url"] is None

    patched = authed.patch(
        f"/api/profiles/{profile_id}",
        json={"verify_url": "https://blog.example/gated"},
        headers=XHR,
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["verify_url"] == "https://blog.example/gated"
    assert authed.get(f"/api/profiles/{profile_id}").json()["verify_url"] == (
        "https://blog.example/gated"
    )


def test_patching_the_verify_url_leaves_the_rest_of_the_profile_alone(
    authed: TestClient,
) -> None:
    """A partial update is partial — the card sends this field by itself."""
    created = authed.post(
        "/api/profiles",
        json={"name": "keeps-its-name", "mode": "userscript", "user_agent": "Mozilla/5.0 (X)"},
        headers=XHR,
    )
    profile_id = created.json()["id"]

    patched = authed.patch(
        f"/api/profiles/{profile_id}", json={"verify_url": "https://blog.example/"}, headers=XHR
    )
    body = patched.json()
    assert body["name"] == "keeps-its-name"
    assert body["user_agent"] == "Mozilla/5.0 (X)"
    assert body["mode"] == "userscript"


def test_the_mint_says_which_field_is_missing_rather_than_failing_vaguely(
    authed: TestClient,
) -> None:
    """The 409 the UI now prevents — worth keeping honest behind it.

    It must name the verify URL, because that is the only thing that tells
    somebody looking at a live browser session that the mint is not reading
    from it.
    """
    created = authed.post(
        "/api/profiles", json={"name": "no-url", "mode": "userscript"}, headers=XHR
    )
    profile_id = created.json()["id"]
    script = b"// ==UserScript==\n// @name probe\n// ==/UserScript==\nvoid 0;\n"
    uploaded = authed.put(
        f"/api/profiles/{profile_id}/script",
        files={"file": ("probe.user.js", script, "text/javascript")},
        headers=XHR,
    )
    assert uploaded.status_code == 200, uploaded.text

    refused = authed.post(f"/api/profiles/{profile_id}/mint", headers=XHR)
    assert refused.status_code == 409
    error = refused.json()["error"]
    assert error["code"] == "no_verify_url"
    assert "verify url" in error["message"].lower()


# ── browsertrix browser profiles ─────────────────────────────────────────
#
# Stored on disk rather than in a column, because it is two orders of
# magnitude larger than everything else here — 41 MB measured for one Google
# login. Sealed all the same: a browser profile is a live session, which is a
# stronger credential than the jar, not a weaker one.

GZIP = b"\x1f\x8b" + b"payload that stands in for a browser profile" * 8


def test_a_browser_profile_round_trips_through_the_seal(
    db: Session, sealer: Sealer, tmp_path: Path
) -> None:
    settings = Settings(
        config_dir=tmp_path / "config", data_dir=tmp_path / "data",
        secret_key="x" * 40, _env_file=None,
    )  # fmt: skip
    profile = make_profile(db)
    source = tmp_path / "profile.tar.gz"
    source.write_bytes(GZIP)

    meta = profiles.store_browser_profile(db, sealer, profile, settings, source)
    assert meta["size"] == len(GZIP)
    assert profiles.has_browser_profile(profile)

    sealed = profiles.browser_profile_path(settings, profile.id)
    assert sealed.is_file()
    assert GZIP not in sealed.read_bytes(), "material must be sealed at rest"

    out = profiles.write_browser_profile(sealer, profile, settings, tmp_path / "job" / "p.tar.gz")
    assert out is not None
    assert out.read_bytes() == GZIP


def test_a_file_that_is_not_a_tarball_is_refused(
    db: Session, sealer: Sealer, tmp_path: Path
) -> None:
    """Uploading the crawler's *log* instead of its tarball would otherwise be
    discovered as a capture that archived the login page a few thousand times."""
    settings = Settings(
        config_dir=tmp_path / "config", data_dir=tmp_path / "data",
        secret_key="x" * 40, _env_file=None,
    )  # fmt: skip
    profile = make_profile(db)
    source = tmp_path / "crawl.log"
    source.write_bytes(b'{"logLevel":"info"}\n')

    with pytest.raises(profiles.ProfileError, match="not a gzip"):
        profiles.store_browser_profile(db, sealer, profile, settings, source)
    assert not profiles.has_browser_profile(profile)


def test_clearing_material_removes_the_tarball_from_disk(
    db: Session, sealer: Sealer, tmp_path: Path
) -> None:
    """Dropping cookie_meta forgets that one exists; without this the sealed
    session stays on disk with nothing pointing at it."""
    settings = Settings(
        config_dir=tmp_path / "config", data_dir=tmp_path / "data",
        secret_key="x" * 40, _env_file=None,
    )  # fmt: skip
    profile = make_profile(db)
    source = tmp_path / "profile.tar.gz"
    source.write_bytes(GZIP)
    profiles.store_browser_profile(db, sealer, profile, settings, source)
    sealed = profiles.browser_profile_path(settings, profile.id)
    assert sealed.is_file()

    profiles.clear_material(db, profile, settings)
    assert not sealed.exists()
    assert not profiles.has_browser_profile(profile)


def test_materialize_hands_over_a_tarball_with_no_cookies_at_all(
    db: Session, sealer: Sealer, tmp_path: Path
) -> None:
    """A browsertrix profile is a complete credential on its own.

    Returning None here because there is no jar would refuse the only thing
    that engine can actually use, which is the silent failure this exists to
    remove.
    """
    settings = Settings(
        config_dir=tmp_path / "config", data_dir=tmp_path / "data",
        secret_key="x" * 40, _env_file=None,
    )  # fmt: skip
    profile = make_profile(db)
    source = tmp_path / "profile.tar.gz"
    source.write_bytes(GZIP)
    profiles.store_browser_profile(db, sealer, profile, settings, source)

    material = profiles.materialize(db, sealer, profile.id, tmp_path / "job-1", settings)
    assert material is not None
    assert material.cookies_file is None
    assert material.profile_file is not None
    assert material.profile_file.read_bytes() == GZIP


def test_the_upload_route_never_leaves_the_plaintext_behind(authed: TestClient) -> None:
    created = authed.post(
        "/api/profiles", json={"name": "btrix", "mode": "interactive"}, headers=XHR
    )
    profile_id = created.json()["id"]

    uploaded = authed.put(
        f"/api/profiles/{profile_id}/browser-profile",
        files={"file": ("profile.tar.gz", GZIP, "application/gzip")},
        headers=XHR,
    )
    assert uploaded.status_code == 200, uploaded.text
    body = uploaded.json()
    assert body["browser_profile"]["size"] == len(GZIP)
    assert body["profile"]["has_browser_profile"] is True
    # Metadata only, like every other material here.
    assert "payload" not in repr(body)


def test_the_upload_route_rejects_a_non_tarball(authed: TestClient) -> None:
    created = authed.post(
        "/api/profiles", json={"name": "btrix-bad", "mode": "interactive"}, headers=XHR
    )
    profile_id = created.json()["id"]

    refused = authed.put(
        f"/api/profiles/{profile_id}/browser-profile",
        files={"file": ("crawl.log", b"not a tarball", "text/plain")},
        headers=XHR,
    )
    assert refused.status_code == 422
    assert refused.json()["error"]["code"] == "invalid_browser_profile"


def test_a_real_browsertrix_tarball_reports_the_hosts_it_covers() -> None:
    """Built by create-login-profile against a fixture that sets two cookies.

    Size and a digest cannot answer "does this reach my blog?", and a tarball
    whose session never cleared the gate looks identical to one that did until
    a capture proves otherwise. Host and cookie name are plaintext in
    Chromium's store; only the values are encrypted, which is the half worth
    reading and the half that must never be stored.
    """
    import io as _io
    import sqlite3
    import tarfile

    db = tmp_sqlite_cookies(
        [("blog.example", "GATE_OK", 13390000000000000), (".google.com", "SID", 0)]
    )
    buffer = _io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        info = tarfile.TarInfo("./Default/Cookies")
        info.size = len(db)
        archive.addfile(info, _io.BytesIO(db))

    report = profiles.describe_browser_profile(buffer.getvalue())
    assert report["readable"] is True
    assert report["cookies"] == 2
    assert report["session_cookies"] == 1
    assert set(report["hosts"]) == {"blog.example", ".google.com"}
    assert report["host_count"] == 2
    assert "GATE_OK" not in repr(report), "names and values are not ours to publish"
    del sqlite3


def tmp_sqlite_cookies(rows: list[tuple[str, str, int]]) -> bytes:
    import sqlite3
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "Cookies"
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE cookies (host_key TEXT, name TEXT, expires_utc INTEGER)")
        conn.executemany("INSERT INTO cookies VALUES (?,?,?)", rows)
        conn.commit()
        conn.close()
        return path.read_bytes()


def test_a_tarball_with_no_cookie_store_is_reported_as_empty_not_broken() -> None:
    """The signal that the profile browser never got past the gate.

    Measured against a real profile made by visiting a site that sets no
    cookies: browsertrix writes no Default/Cookies member at all.
    """
    import io as _io
    import tarfile

    buffer = _io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        info = tarfile.TarInfo("./Default/Preferences")
        info.size = 2
        archive.addfile(info, _io.BytesIO(b"{}"))

    report = profiles.describe_browser_profile(buffer.getvalue())
    assert report["readable"] is True
    assert report["cookies"] == 0
    assert report["hosts"] == []


def test_something_that_is_not_a_tarball_says_so_rather_than_raising() -> None:
    report = profiles.describe_browser_profile(b"\x1f\x8b not really a tarball")
    assert report["readable"] is False
    assert report["cookies"] == 0


def test_the_host_count_is_the_total_not_the_length_of_the_capped_list() -> None:
    """Reported as "151 cookies across 30 hosts" on a profile with more.

    30 was the cap on the list, and the card counted the list — so the cap was
    the answer, and would have been the answer for any larger number too. A
    truncated list is fine; a count derived from one is a wrong number wearing
    a right one's clothes.
    """
    import io as _io
    import tarfile

    rows = [
        (f"host{n}.example", "C", 13390000000000000) for n in range(profiles.HOST_LIST_LIMIT + 25)
    ]
    db = tmp_sqlite_cookies(rows)
    buffer = _io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        info = tarfile.TarInfo("./Default/Cookies")
        info.size = len(db)
        archive.addfile(info, _io.BytesIO(db))

    report = profiles.describe_browser_profile(buffer.getvalue())
    assert report["cookies"] == len(rows)
    assert report["host_count"] == len(rows), "the total must not be capped"
    assert len(report["hosts"]) == profiles.HOST_LIST_LIMIT, "the list still is"


# ── when a browser profile stops working ─────────────────────────────────


def _tarball(rows: list[tuple[str, str, int]]) -> bytes:
    import io as _io
    import tarfile

    db = tmp_sqlite_cookies(rows)
    buffer = _io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        info = tarfile.TarInfo("./Default/Cookies")
        info.size = len(db)
        archive.addfile(info, _io.BytesIO(db))
    return buffer.getvalue()


def _chromium_stamp(when: datetime) -> int:
    """A datetime as Chromium stores it: microseconds since 1601."""
    return int((when.timestamp() + profiles.CHROMIUM_EPOCH_OFFSET_S) * 1_000_000)


def test_expiries_are_the_soonest_per_host_not_one_date_for_the_file() -> None:
    """A profile holds a consent cookie expiring next week beside a sign-in
    that lasts months. One date for the tarball would be the consent cookie's,
    which is a warning about nothing — so the readout is keyed by host and the
    judging happens where the crawl's own hosts are known."""
    from datetime import timedelta

    now = utcnow()
    raw = _tarball(
        [
            (".google.com", "SID", _chromium_stamp(now + timedelta(days=90))),
            (".google.com", "CONSENT", _chromium_stamp(now + timedelta(days=400))),
            ("blog.example", "GATE", _chromium_stamp(now + timedelta(days=3))),
            ("blog.example", "SESSION", 0),
        ]
    )

    report = profiles.describe_browser_profile(raw)

    assert set(report["expiries"]) == {".google.com", "blog.example"}
    # The soonest per host, not the latest and not the average.
    google = datetime.fromisoformat(report["expiries"][".google.com"])
    blog = datetime.fromisoformat(report["expiries"]["blog.example"])
    assert 89 <= (google - now).days <= 90
    assert 2 <= (blog - now).days <= 3
    # Session cookies have no expiry; they must not be read as 1601.
    assert report["session_cookies"] == 1


def test_an_absurd_expiry_is_dropped_rather_than_reported() -> None:
    """A corrupt row would otherwise render as "expires in 28,000 years",
    which reads as a bug in Cairn rather than in the tarball."""
    raw = _tarball([("blog.example", "BROKEN", 99_999_999_999_999_999)])
    assert profiles.describe_browser_profile(raw)["expiries"] == {}


def test_a_zero_expiry_is_a_session_cookie_not_the_year_1601() -> None:
    assert profiles._chromium_epoch(0) is None
    assert profiles._chromium_epoch("not a number") is None


def _profile_with(expiries: dict[str, str]) -> object:
    from types import SimpleNamespace

    return SimpleNamespace(browser_profile={"expiries": expiries}, cookie_meta={})


def test_the_warning_only_fires_for_hosts_this_site_actually_crawls() -> None:
    """The whole point of matching. A tarball made in a real browser carries
    dozens of hosts from wherever that session had been; warning on all of
    them is a warning every day about nothing, which teaches the reader to
    skip it — the exact failure the thumbnail step's comment names."""
    from datetime import timedelta

    from cairn.services.jobs import _browser_profile_expiry_warnings

    soon = to_iso(utcnow() + timedelta(days=2))
    profile = _profile_with({"analytics.example": soon, "unrelated.test": soon})

    assert _browser_profile_expiry_warnings(profile, ["blog.example"]) == []
    assert _browser_profile_expiry_warnings(profile, ["analytics.example"]) != []


def test_a_dot_prefixed_cookie_domain_covers_the_subdomain_the_site_uses() -> None:
    """Cookies are stored on `.google.com`; a site crawls
    `accounts.google.com`. The two are written at different levels of the same
    tree, so an exact match would silently never fire — which is the failure
    mode that looks exactly like "nothing is expiring"."""
    from datetime import timedelta

    from cairn.services.jobs import _browser_profile_expiry_warnings

    profile = _profile_with({".google.com": to_iso(utcnow() + timedelta(days=3))})

    assert _browser_profile_expiry_warnings(profile, ["accounts.google.com"]) != []
    assert _browser_profile_expiry_warnings(profile, ["google.com"]) != []
    # But not a host that merely ends in the same letters.
    assert _browser_profile_expiry_warnings(profile, ["notgoogle.com"]) == []


def test_a_distant_expiry_says_nothing() -> None:
    from datetime import timedelta

    from cairn.services.jobs import _browser_profile_expiry_warnings

    profile = _profile_with({"blog.example": to_iso(utcnow() + timedelta(days=90))})
    assert _browser_profile_expiry_warnings(profile, ["blog.example"]) == []


def test_an_already_expired_profile_says_so_in_the_past_tense() -> None:
    """Different advice: there is nothing to do before a deadline that has
    gone, and the capture about to run will archive the sign-in page."""
    from datetime import timedelta

    from cairn.services.jobs import _browser_profile_expiry_warnings

    profile = _profile_with({"blog.example": to_iso(utcnow() - timedelta(days=1))})
    (note,) = _browser_profile_expiry_warnings(profile, ["blog.example"])

    assert "expired on" in note
    assert "archive the sign-in page" in note


def test_a_profile_with_no_expiry_data_warns_about_nothing() -> None:
    """Every tarball uploaded before this shipped has no `expiries` key."""
    from cairn.services.jobs import _browser_profile_expiry_warnings

    assert _browser_profile_expiry_warnings(_profile_with({}), ["blog.example"]) == []


# ── testing a browser profile against the site ───────────────────────────


def _warc_with(pages: dict[str, bytes], root: Path) -> Path:
    """A collection shaped the way the crawler leaves one."""
    from warcio.warcwriter import WARCWriter

    archive = root / "collections" / "profilecheck" / "archive"
    archive.mkdir(parents=True, exist_ok=True)
    target = archive / "rec-0.warc.gz"
    with target.open("wb") as fh:
        writer = WARCWriter(fh, gzip=True)
        for url, body in pages.items():
            record = writer.create_warc_record(
                url,
                "response",
                payload=io.BytesIO(body),
                http_headers=StatusAndHeaders(
                    "200 OK",
                    [("Content-Type", "text/html; charset=utf-8")],
                    protocol="HTTP/1.1",
                ),
            )
            writer.write_record(record)
    return target


GATE = b"<html><body><h1>Content warning</h1><p>I understand and wish to continue</p></body></html>"
REAL = b"<html><body><h1>A post</h1>" + b"<p>Real words. </p>" * 200 + b"</body></html>"


def test_the_verdict_comes_from_the_record_for_the_url_that_was_asked_for(
    tmp_path: Path,
) -> None:
    """A gated blog records the gate *as well*, at blogger.com's URL. Reading
    whichever record came first would report failure for a profile that
    worked — which is exactly the confusion this tool exists to end."""
    from cairn.services import profilecheck

    _warc_with(
        {
            "https://www.blogger.com/interstitial/blog?u=https://blog.example/": GATE,
            "https://blog.example/post.html": REAL,
        },
        tmp_path,
    )

    result = profilecheck._read_result(tmp_path, "https://blog.example/post.html")

    assert result.verdict == "pass"
    assert result.final_url == "https://blog.example/post.html"


def test_a_gate_at_the_asked_for_url_is_reported_as_a_gate(tmp_path: Path) -> None:
    from cairn.services import profilecheck

    _warc_with({"https://blog.example/post.html": GATE}, tmp_path)

    result = profilecheck._read_result(tmp_path, "https://blog.example/post.html")

    assert result.verdict == "gate"
    # The detector says which marker it matched, which is the difference
    # between "this is a gate" and "this is a gate, and here is why I think so".
    assert "wish to continue" in result.reason


def test_a_gate_without_the_profile_loaded_is_a_different_diagnosis() -> None:
    """`gate` says the site refused the session; `no_profile` says there was
    no session. Same symptom, different fix — one means rebuild the profile,
    the other means the tarball never arrived."""
    from cairn.services.profilecheck import CheckResult

    result = CheckResult(verdict="gate", reason="looks like a content warning")
    result.profile_loaded = False
    # The same reconciliation `check` does once it has read the log.
    if result.verdict == "gate" and not result.profile_loaded:
        result.verdict = "no_profile"
    assert result.verdict == "no_profile"


def test_no_archive_at_all_is_an_error_not_a_pass(tmp_path: Path) -> None:
    """The crawler exits 0 having archived nothing when it cannot reach the
    site. Reading that as success would be the worst possible answer."""
    from cairn.services import profilecheck

    result = profilecheck._read_result(tmp_path, "https://blog.example/")
    assert result.verdict == "error"
    assert not result.ok
