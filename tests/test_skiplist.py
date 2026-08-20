"""The skip list that applies to every site.

Two properties carry this feature and both are easy to lose:

  1. A pattern added here reaches sites that already exist, and removing it
     takes it away from them again. That only holds while the list is merged
     at resolve time; the moment anything copies it into a site's own rows,
     "remove" silently becomes "stop giving it to new sites".
  2. A site can excuse itself from one entry without the list losing it.

The round-trip test is the one that would catch a regression in (1) — it does
what the domain picker does, which is read a scope and post it straight back.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from cairn.config import Settings
from cairn.db.models import Site
from cairn.services import sites as site_service
from cairn.services import skiplist
from cairn.services.scope import HostRule, Scope, ScopeError, build_reject_patterns
from tests.conftest import XHR


def make_site(db: Session, settings: Settings, host: str = "example.com") -> Site:
    site = site_service.create_site(db, settings, seed_url=f"https://{host}/", folder_id=1)
    site_service.save_scope(
        db,
        site,
        Scope(
            seeds=[f"https://{host}/"],
            hosts=[HostRule(host, crawl_pages=True, fetch_assets=True)],
            reject_patterns=[r"[?&]m=1"],
        ),
    )
    db.flush()
    return site


# ── the list itself ──────────────────────────────────────────────────────


def test_an_unset_list_is_empty_rather_than_missing(db: Session) -> None:
    assert skiplist.load(db) == []


def test_add_and_remove_round_trip(db: Session) -> None:
    skiplist.add(db, r"[?&]utm_source=")
    skiplist.add(db, r"[?&]fbclid=")
    assert skiplist.load(db) == [r"[?&]utm_source=", r"[?&]fbclid="]

    skiplist.remove(db, r"[?&]utm_source=")
    assert skiplist.load(db) == [r"[?&]fbclid="]


def test_adding_the_same_pattern_twice_leaves_one(db: Session) -> None:
    skiplist.add(db, r"[?&]utm_source=")
    skiplist.add(db, r"[?&]utm_source=")
    assert skiplist.load(db) == [r"[?&]utm_source="]


def test_removing_something_absent_is_not_an_error(db: Session) -> None:
    skiplist.add(db, r"[?&]utm_source=")
    skiplist.remove(db, r"never-added")
    assert skiplist.load(db) == [r"[?&]utm_source="]


@pytest.mark.parametrize("bad", ["[unclosed", "*", "(?P<", "a{2,1}"])
def test_an_uncompilable_pattern_is_refused(db: Session, bad: str) -> None:
    """A bad regex here would break every capture on the instance at once."""
    with pytest.raises(skiplist.SkipListError):
        skiplist.add(db, bad)
    assert skiplist.load(db) == []


def test_blank_entries_are_dropped_rather_than_stored(db: Session) -> None:
    assert skiplist.save(db, ["  ", "", r"[?&]m=1", "   "]) == [r"[?&]m=1"]


def test_the_list_has_a_ceiling(db: Session) -> None:
    with pytest.raises(skiplist.SkipListError):
        skiplist.save(db, [f"pattern-{n}" for n in range(skiplist.MAX_PATTERNS + 1)])


# ── how it reaches a capture ─────────────────────────────────────────────


def test_a_global_pattern_reaches_a_site_that_already_existed(
    db: Session, settings: Settings
) -> None:
    site = make_site(db, settings)
    assert site_service.resolved_scope(db, site).global_reject_patterns == []

    skiplist.add(db, r"[?&]utm_source=")

    scope = site_service.resolved_scope(db, site)
    assert scope.global_reject_patterns == [r"[?&]utm_source="]
    # And it is what the engine will actually enforce.
    assert r"[?&]utm_source=" in build_reject_patterns(scope)


def test_removing_a_global_pattern_removes_it_from_every_site(
    db: Session, settings: Settings
) -> None:
    one = make_site(db, settings, "one.example.com")
    two = make_site(db, settings, "two.example.com")
    skiplist.add(db, r"[?&]utm_source=")
    assert site_service.resolved_scope(db, one).global_reject_patterns
    assert site_service.resolved_scope(db, two).global_reject_patterns

    skiplist.remove(db, r"[?&]utm_source=")

    assert site_service.resolved_scope(db, one).global_reject_patterns == []
    assert site_service.resolved_scope(db, two).global_reject_patterns == []


def test_a_global_pattern_never_becomes_one_of_the_sites_own(
    db: Session, settings: Settings
) -> None:
    site = make_site(db, settings)
    skiplist.add(db, r"[?&]utm_source=")
    scope = site_service.resolved_scope(db, site)
    assert scope.reject_patterns == [r"[?&]m=1"]


def test_a_bad_global_pattern_is_reported_against_the_scope_not_swallowed() -> None:
    """Only reachable by hand-editing the database, but it must be loud.

    Silently dropping it would mean a capture running with a boundary nobody
    can see in the settings that supposedly describe it.
    """
    scope = Scope(
        seeds=["https://example.com/"],
        hosts=[HostRule("example.com", crawl_pages=True)],
        global_reject_patterns=["[unclosed"],
    )
    with pytest.raises(ScopeError):
        scope.validate()


# ── the per-site escape hatch ────────────────────────────────────────────


def test_a_site_can_excuse_itself_from_one_pattern(db: Session, settings: Settings) -> None:
    site = make_site(db, settings)
    skiplist.save(db, [r"[?&]utm_source=", r"[?&]fbclid="])

    site_service.set_global_reject_exceptions(db, site, [r"[?&]fbclid="])

    scope = site_service.resolved_scope(db, site)
    assert scope.global_reject_patterns == [r"[?&]utm_source="]
    enforced = build_reject_patterns(scope)
    # Both halves, or "not in" passes just as well when the merge is gone
    # entirely and the test proves nothing.
    assert r"[?&]utm_source=" in enforced
    assert r"[?&]fbclid=" not in enforced


def test_one_sites_exception_does_not_reach_another(db: Session, settings: Settings) -> None:
    one = make_site(db, settings, "one.example.com")
    two = make_site(db, settings, "two.example.com")
    skiplist.add(db, r"[?&]fbclid=")
    site_service.set_global_reject_exceptions(db, one, [r"[?&]fbclid="])

    assert site_service.resolved_scope(db, one).global_reject_patterns == []
    assert site_service.resolved_scope(db, two).global_reject_patterns == [r"[?&]fbclid="]


def test_an_exception_survives_the_pattern_being_removed_and_put_back(
    db: Session, settings: Settings
) -> None:
    """Otherwise turning a global rule off and on again re-applies it to the
    sites that had opted out — the quietest possible way to widen a scope."""
    site = make_site(db, settings)
    skiplist.add(db, r"[?&]fbclid=")
    site_service.set_global_reject_exceptions(db, site, [r"[?&]fbclid="])

    skiplist.remove(db, r"[?&]fbclid=")
    assert site_service.resolved_scope(db, site).global_reject_patterns == []

    skiplist.add(db, r"[?&]fbclid=")
    assert site_service.resolved_scope(db, site).global_reject_patterns == []


def test_saving_a_scope_does_not_drop_the_exceptions(db: Session, settings: Settings) -> None:
    site = make_site(db, settings)
    skiplist.add(db, r"[?&]fbclid=")
    site_service.set_global_reject_exceptions(db, site, [r"[?&]fbclid="])

    site_service.save_scope(
        db,
        site,
        Scope(
            seeds=["https://example.com/"],
            hosts=[HostRule("example.com", crawl_pages=True, fetch_assets=True)],
            reject_patterns=[r"[?&]m=1"],
        ),
    )
    assert site_service.global_reject_exceptions(site) == [r"[?&]fbclid="]


def test_a_global_pattern_survives_the_job_spec_and_reaches_both_engines(
    tmp_path: Path,
) -> None:
    """The whole path: resolved scope → `to_dict` → job spec → engine flags.

    A crawl has one boundary whichever engine walks it
    (`test_both_engines_enforce_the_same_reject_set`), and a pattern that
    reached only one of them would be the quietest way to break that — the
    symptom is a bigger crawl on one engine, never an error.
    """
    from cairn.engines.browsertrix import Runner
    from cairn.engines.protocol import EventWriter
    from cairn.engines.wget import build_argv
    from tests.test_engines import wget_spec

    resolved = Scope(
        seeds=["https://example.blogspot.com/"],
        hosts=[HostRule("example.blogspot.com", crawl_pages=True, fetch_assets=True)],
        reject_patterns=[r"[?&]m=1"],
        global_reject_patterns=[r"[?&]utm_[a-z]+="],
    )
    # Serialized and re-read, because that is what happens between the
    # supervisor and the engine — a field missing from `to_dict` would vanish
    # here and nowhere else.
    spec = wget_spec(tmp_path, scope=resolved.to_dict())

    wget = build_argv(spec, tmp_path / "out", tmp_path / "tmp", tmp_path / "tmp")
    wget_regex = next(a.split("=", 1)[1] for a in wget if a.startswith("--reject-regex="))

    argv = Runner(spec, EventWriter())._argv()
    browsertrix_regex = argv[argv.index("--blockRules") + 1]

    assert wget_regex == browsertrix_regex
    compiled = re.compile(wget_regex)
    assert compiled.search("https://example.blogspot.com/p.html?utm_source=x")
    assert compiled.search("https://example.blogspot.com/p.html?m=1")
    assert not compiled.search("https://example.blogspot.com/p.html")


# ── through the API ──────────────────────────────────────────────────────


def test_the_list_round_trips_through_the_api(authed: TestClient) -> None:
    put = authed.put(
        "/api/crawl/skip-patterns",
        json={"patterns": [r"[?&]utm_source=", r"[?&]fbclid="]},
        headers=XHR,
    )
    assert put.status_code == 200, put.text
    assert put.json()["patterns"] == [r"[?&]utm_source=", r"[?&]fbclid="]

    got = authed.get("/api/crawl/skip-patterns", headers=XHR)
    assert got.json()["patterns"] == [r"[?&]utm_source=", r"[?&]fbclid="]


def test_the_api_refuses_a_pattern_that_would_not_compile(authed: TestClient) -> None:
    response = authed.put("/api/crawl/skip-patterns", json={"patterns": ["[unclosed"]}, headers=XHR)
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "invalid_pattern"
    # And nothing was stored, so the next capture is unaffected.
    assert authed.get("/api/crawl/skip-patterns", headers=XHR).json()["patterns"] == []


def test_reading_a_scope_and_posting_it_back_does_not_absorb_the_global_list(
    authed: TestClient,
) -> None:
    """What the domain picker does on every save.

    If the scope response returned the merged list, one save would copy the
    global patterns into the site's own rows — and they would then survive
    being removed from Settings, with nothing to say where they came from.
    """
    created = authed.post("/api/sites", json={"seed_url": "https://absorb.example/"}, headers=XHR)
    site_id = created.json()["id"]
    authed.put("/api/crawl/skip-patterns", json={"patterns": [r"[?&]utm_source="]}, headers=XHR)

    scope = authed.get(f"/api/sites/{site_id}/scope", headers=XHR).json()
    assert scope["reject_patterns"] == []
    assert scope["global_reject_patterns"] == [r"[?&]utm_source="]

    saved = authed.put(
        f"/api/sites/{site_id}/scope",
        json={
            "hosts": scope["hosts"],
            "reject_patterns": scope["reject_patterns"],
            "global_reject_exceptions": scope["global_reject_exceptions"],
            "obey_robots": scope["obey_robots"],
            "max_pages": scope["max_pages"],
            "max_bytes": scope["max_bytes"],
            "politeness": scope["politeness"],
        },
        headers=XHR,
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["reject_patterns"] == []

    # Now take it away globally. If the save had absorbed it, it would still
    # be here.
    authed.put("/api/crawl/skip-patterns", json={"patterns": []}, headers=XHR)
    after = authed.get(f"/api/sites/{site_id}/scope", headers=XHR).json()
    assert after["reject_patterns"] == []
    assert after["global_reject_patterns"] == []


def test_an_exception_round_trips_through_the_scope_endpoint(authed: TestClient) -> None:
    created = authed.post("/api/sites", json={"seed_url": "https://except.example/"}, headers=XHR)
    site_id = created.json()["id"]
    authed.put(
        "/api/crawl/skip-patterns",
        json={"patterns": [r"[?&]utm_source=", r"[?&]fbclid="]},
        headers=XHR,
    )

    scope = authed.get(f"/api/sites/{site_id}/scope", headers=XHR).json()
    saved = authed.put(
        f"/api/sites/{site_id}/scope",
        json={
            "hosts": scope["hosts"],
            "reject_patterns": [],
            "global_reject_exceptions": [r"[?&]fbclid="],
            "obey_robots": True,
            "max_pages": None,
            "max_bytes": None,
            "politeness": {},
        },
        headers=XHR,
    )
    assert saved.status_code == 200, saved.text
    body = saved.json()
    # The whole list is still reported — the picker has to show a rule to
    # offer switching it back on.
    assert body["global_reject_patterns"] == [r"[?&]utm_source=", r"[?&]fbclid="]
    assert body["global_reject_exceptions"] == [r"[?&]fbclid="]
