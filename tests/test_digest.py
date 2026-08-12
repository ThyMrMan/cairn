"""The periodic report.

Every test here is about *absence*, because that is what the feature is for.
An archiver that fails loudly already has a notification; the one that stops
quietly — a feed that polls and returns nothing, a site nothing has captured
since March, a cookie jar that expired last Tuesday — has nothing at all, and
is the reason somebody discovers three weeks later that a blog they meant to
keep was never archived.
"""

from __future__ import annotations

from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from cairn.config import Settings
from cairn.db.models import AccessProfile, Capture, Feed, FeedItem, Job, Site
from cairn.db.types import utcnow
from cairn.services import digest as digest_service
from cairn.services import notify, settings_store
from cairn.services import sites as site_service
from tests.conftest import XHR


def _site(db: Session, settings: Settings, title: str, **kwargs: object) -> Site:
    site = site_service.create_site(
        db, settings, seed_url=f"https://{title}.example.com/", title=title
    )
    for key, value in kwargs.items():
        setattr(site, key, value)
    db.flush()
    return site


def _window(days: int = 7) -> dict[str, object]:
    now = utcnow()
    return {"since": now - timedelta(days=days), "now": now}


# ── what happened ────────────────────────────────────────────────────────


def test_captures_inside_the_window_are_counted(db: Session, settings: Settings) -> None:
    site = _site(db, settings, "counted")
    now = utcnow()
    for status, when in (("ok", now - timedelta(days=1)), ("failed", now - timedelta(days=2))):
        db.add(
            Capture(
                site_id=site.id,
                kind="full",
                engine_id="wget-warc",
                dir_name=f"{status}-{when.timestamp()}",
                started_at=when,
                status=status,
                url_count=10,
                bytes_written=1000,
            )
        )
    # Outside the window, and must not be counted.
    db.add(
        Capture(
            site_id=site.id,
            kind="full",
            engine_id="wget-warc",
            dir_name="ancient",
            started_at=now - timedelta(days=90),
            status="ok",
            url_count=999,
        )
    )
    db.flush()

    report = digest_service.build(db, settings, **_window())  # type: ignore[arg-type]
    assert report.captures_ok == 1
    assert report.captures_failed == 1
    assert report.urls_archived == 20


def test_a_failed_job_is_named_not_merely_counted(db: Session, settings: Settings) -> None:
    """A count sends the reader to the job list to find out what it counted."""
    site = _site(db, settings, "broken")
    db.add(
        Job(
            type="capture",
            site_id=site.id,
            status="failed",
            finished_at=utcnow() - timedelta(hours=2),
            error="wget exited 8",
        )
    )
    db.flush()

    report = digest_service.build(db, settings, **_window())  # type: ignore[arg-type]
    assert len(report.failed_jobs) == 1
    assert report.failed_jobs[0]["site"] == "broken"
    assert "wget exited 8" in report.failed_jobs[0]["error"]
    assert "wget exited 8" in digest_service.render_text(report)


# ── what quietly did not ─────────────────────────────────────────────────


def test_a_site_nobody_has_captured_in_months_is_reported(
    db: Session, settings: Settings
) -> None:
    now = utcnow()
    _site(db, settings, "recent", last_capture_at=now - timedelta(days=2))
    _site(db, settings, "forgotten", last_capture_at=now - timedelta(days=120))

    report = digest_service.build(db, settings, **_window())  # type: ignore[arg-type]
    assert [s["title"] for s in report.quiet_sites] == ["forgotten"]
    assert report.quiet_sites[0]["days"] >= 119
    assert report.has_problems


def test_a_feed_that_polls_and_returns_nothing_is_reported(
    db: Session, settings: Settings
) -> None:
    """The failure mode with no failure.

    The fetch succeeds, the parse succeeds, and there are simply no entries —
    because the URL now serves a login page, or the blog moved. Nothing in the
    system raises anything about it.
    """
    now = utcnow()
    live = _site(db, settings, "live")
    dead = _site(db, settings, "dead")

    healthy = Feed(site_id=live.id, url="https://live.example.com/feed", last_polled_at=now)
    stalled = Feed(site_id=dead.id, url="https://dead.example.com/feed", last_polled_at=now)
    db.add_all([healthy, stalled])
    db.flush()

    db.add(
        FeedItem(
            feed_id=healthy.id,
            guid="a",
            url="https://live.example.com/1",
            first_seen_at=now - timedelta(days=1),
            last_seen_at=now,
        )
    )
    db.add(
        FeedItem(
            feed_id=stalled.id,
            guid="b",
            url="https://dead.example.com/1",
            first_seen_at=now - timedelta(days=200),
            last_seen_at=now - timedelta(days=200),
        )
    )
    db.flush()

    report = digest_service.build(db, settings, **_window())  # type: ignore[arg-type]
    assert [f["site"] for f in report.stalled_feeds] == ["dead"]


def test_a_feed_that_was_turned_off_is_left_to_its_own_notification(
    db: Session, settings: Settings
) -> None:
    """Disabled feeds already have an alert; repeating it here is noise."""
    now = utcnow()
    site = _site(db, settings, "offsite")
    db.add(
        Feed(
            site_id=site.id,
            url="https://offsite.example.com/feed",
            enabled=False,
            last_polled_at=now,
        )
    )
    db.flush()
    report = digest_service.build(db, settings, **_window())  # type: ignore[arg-type]
    assert report.stalled_feeds == []


def test_credentials_about_to_expire_are_mentioned(db: Session, settings: Settings) -> None:
    now = utcnow()
    db.add(AccessProfile(name="soon", mode="cookies", expires_at=now + timedelta(days=5)))
    db.add(AccessProfile(name="already", mode="cookies", expires_at=now - timedelta(days=1)))
    db.add(AccessProfile(name="fine", mode="cookies", expires_at=now + timedelta(days=200)))
    db.flush()

    report = digest_service.build(db, settings, **_window())  # type: ignore[arg-type]
    names = {p["name"]: p["expired"] for p in report.expiring_profiles}
    assert names == {"already": True, "soon": False}


# ── growth and rendering ─────────────────────────────────────────────────


def test_growth_is_the_difference_between_two_readings(
    db: Session, settings: Settings
) -> None:
    """Not a sum over captures.

    Summing captures would count everything that arrived and nothing that
    left, so an archive that retention had pruned by 10 GB would still report
    a week of growth.
    """
    _site(db, settings, "grower", size_bytes=5_000_000_000)
    report = digest_service.build(
        db, settings, previous_total=3_000_000_000, **_window()  # type: ignore[arg-type]
    )
    assert report.total_bytes == 5_000_000_000
    assert report.growth_bytes == 2_000_000_000
    assert "+1.9 GB" in digest_service.render_text(report)


def test_a_quiet_period_says_so_in_one_line(db: Session, settings: Settings) -> None:
    _site(db, settings, "calm", last_capture_at=utcnow())
    report = digest_service.build(db, settings, **_window())  # type: ignore[arg-type]
    assert not report.has_problems
    assert "Nothing needs attention." in digest_service.render_text(report)


def test_problems_are_rendered_before_the_summary(db: Session, settings: Settings) -> None:
    """Four lines of attention, spent on the part that needs it."""
    _site(db, settings, "stale", last_capture_at=utcnow() - timedelta(days=90))
    report = digest_service.build(db, settings, **_window())  # type: ignore[arg-type]
    text = digest_service.render_text(report)
    assert text.splitlines()[0].startswith("1 site(s) not captured")


# ── the schedule ─────────────────────────────────────────────────────────


def test_the_first_digest_waits_a_full_period(db: Session, settings: Settings) -> None:
    """A report an hour after installation says nothing.

    And teaches the reader that the next one is also worth ignoring, which is
    the only way this feature can actually fail.
    """
    now = utcnow()
    assert digest_service.is_due(db, now=now) is False
    assert digest_service.is_due(db, now=now + timedelta(days=6)) is False
    assert digest_service.is_due(db, now=now + timedelta(days=8)) is True


def test_switching_it_off_stops_it(db: Session, settings: Settings) -> None:
    settings_store.put(db, digest_service.EVERY_DAYS_SETTING, 0)
    assert digest_service.is_due(db, now=utcnow() + timedelta(days=400)) is False


def test_the_window_starts_where_the_last_one_ended(db: Session, settings: Settings) -> None:
    now = utcnow()
    digest_service.is_due(db, now=now)  # stamps the first window
    assert digest_service.window_start(db, now=now + timedelta(days=8)) == now


# ── through the API and the scheduler ────────────────────────────────────


def test_the_report_is_readable_without_configuring_anything(authed: TestClient) -> None:
    """The reason it is an endpoint and not only a notification."""
    response = authed.get("/api/digest?days=30", headers=XHR)
    assert response.status_code == 200, response.text
    body = response.json()
    assert "text" in body
    assert body["days"] == 30.0
    assert set(body["captures"]) == {"ok", "partial", "failed"}


def test_the_digest_is_an_event_that_can_be_switched_off(authed: TestClient) -> None:
    settings = authed.get("/api/notifications", headers=XHR).json()
    assert notify.DIGEST in settings["events"]
    assert settings["events"][notify.DIGEST] is True

    authed.put("/api/notifications", json={"events": {notify.DIGEST: False}}, headers=XHR)
    after = authed.get("/api/notifications", headers=XHR).json()
    assert after["events"][notify.DIGEST] is False


def test_the_interval_round_trips_through_the_schedule_settings(authed: TestClient) -> None:
    current = authed.get("/api/schedule", headers=XHR).json()
    assert current["digest_every_days"] == digest_service.DEFAULT_EVERY_DAYS

    current["digest_every_days"] = 30
    saved = authed.put("/api/schedule", json=current, headers=XHR)
    assert saved.status_code == 200, saved.text
    assert saved.json()["digest_every_days"] == 30


async def test_a_tick_sends_the_digest_when_it_is_due(
    app: object, db: Session, settings: Settings, webhook: tuple[str, list[dict[str, object]]]
) -> None:
    """End to end through the ticker and a real socket."""
    url, received = webhook
    notify.set_targets(db, [{"url": url, "enabled": True}])
    settings_store.put(
        db, digest_service.LAST_SENT_SETTING, (utcnow() - timedelta(days=30)).isoformat()
    )
    _site(db, settings, "silent", last_capture_at=utcnow() - timedelta(days=200))
    db.commit()

    scheduler = app.state.scheduler  # type: ignore[attr-defined]
    report = await scheduler.tick()

    assert "sent the periodic digest" in report.maintenance
    assert len(received) == 1
    assert "silent" in str(received[0]["payload"])

    # And not again on the very next tick.
    received.clear()
    await scheduler.tick()
    assert received == []
