"""The filter, and the one property that keeps saved views honest.

docs/09: *the same filter object serializes into `saved_views.query`… a
divergence between "what the filter bar produces" and "what a saved view
stores" is a bug factory.* The way to stop that being a thing to remember is
to make it a property: anything a filter can express must survive a round trip
through both of its serializations unchanged.
"""

from __future__ import annotations

from urllib.parse import parse_qs

import pytest
from fastapi.testclient import TestClient

from cairn.services.filters import FilterError, SiteFilter
from tests.conftest import XHR

EVERY_FIELD = SiteFilter(
    folder_id=3,
    folder_recursive=False,
    tags=["travel", "photography"],
    tag_mode="any",
    status="ready",
    engine_id="wget-warc",
    profile_id=7,
    host="blogspot.com",
    has_errors=True,
    never_captured=False,
    last_capture_after="2026-01-01T00:00:00+00:00",
    last_capture_before="2026-06-01T00:00:00+00:00",
    size_min=1024,
    size_max=1_073_741_824,
    q="cats",
    sort="title",
)


def test_a_full_filter_survives_the_json_round_trip() -> None:
    assert SiteFilter.from_dict(EVERY_FIELD.to_dict()) == EVERY_FIELD


def test_a_full_filter_survives_the_query_string_round_trip() -> None:
    """The half that actually differs: `tags` goes on the wire as repeated
    `tag`, and booleans as text. If either direction slips, a saved view and
    the filter bar quietly stop meaning the same thing."""
    params = parse_qs(EVERY_FIELD.to_query_string())
    flat = {k: (v if k == "tag" else v[0]) for k, v in params.items()}

    assert SiteFilter.from_params(flat) == EVERY_FIELD


def test_an_empty_filter_serializes_to_nothing() -> None:
    """Only what differs from the default is stored, so a view saved today
    carries no opinion about a field added next year."""
    assert SiteFilter().to_dict() == {}
    assert SiteFilter().to_query_string() == ""
    assert SiteFilter().is_empty


def test_false_is_a_filter_and_unset_is_not() -> None:
    """`has_errors=false` means "only clean sites", which is not the same as
    not filtering on it at all — hence three states rather than a bool."""
    assert SiteFilter.from_params({"has_errors": "false"}).has_errors is False
    assert SiteFilter.from_params({}).has_errors is None
    assert "has_errors" in SiteFilter.from_params({"has_errors": "false"}).to_dict()


def test_tags_accept_both_wire_forms() -> None:
    assert SiteFilter.from_params({"tag": ["a", "b"]}).tags == ["a", "b"]
    assert SiteFilter.from_params({"tags": "a,b"}).tags == ["a", "b"]
    assert SiteFilter.from_params({"tag": ["A", "a"]}).tags == ["a"]


@pytest.mark.parametrize(
    ("params", "message"),
    [
        ({"sort": "; DROP TABLE sites"}, "cannot sort by"),
        ({"folder_id": "abc"}, "must be a number"),
        ({"tag_mode": "either"}, "tag_mode must be"),
        ({"last_capture_after": "last tuesday"}, "not a date"),
    ],
)
def test_nonsense_is_refused_with_something_readable(params: dict[str, str], message: str) -> None:
    with pytest.raises(FilterError, match=message):
        SiteFilter.from_params(params)


# ── against the database ─────────────────────────────────────────────────


def _site(client: TestClient, host: str, **extra: object) -> dict:
    body = {"seed_url": f"https://{host}/", **extra}
    return client.post("/api/sites", json=body, headers=XHR).json()


def test_tag_mode_all_is_an_intersection_and_any_is_a_union(authed: TestClient) -> None:
    _site(authed, "both.example.com", tags=["travel", "food"])
    _site(authed, "one.example.com", tags=["travel"])

    every = authed.get("/api/sites?tag=travel&tag=food&tag_mode=all", headers=XHR).json()
    either = authed.get("/api/sites?tag=travel&tag=food&tag_mode=any", headers=XHR).json()

    assert every["total"] == 1
    assert either["total"] == 2


def test_a_folder_filter_reaches_into_subfolders_unless_told_not_to(
    authed: TestClient,
) -> None:
    blogs = authed.post("/api/folders", json={"name": "Blogs"}, headers=XHR).json()
    photo = authed.post(
        "/api/folders", json={"name": "Photography", "parent_id": blogs["id"]}, headers=XHR
    ).json()
    _site(authed, "top.example.com", folder_id=blogs["id"])
    _site(authed, "deep.example.com", folder_id=photo["id"])

    recursive = authed.get(f"/api/sites?folder_id={blogs['id']}", headers=XHR).json()
    shallow = authed.get(
        f"/api/sites?folder_id={blogs['id']}&folder_recursive=false", headers=XHR
    ).json()

    assert recursive["total"] == 2
    assert shallow["total"] == 1


def test_never_captured_finds_exactly_the_sites_with_no_captures(authed: TestClient) -> None:
    _site(authed, "fresh.example.com")

    listed = authed.get("/api/sites?never_captured=true", headers=XHR).json()

    assert listed["total"] == 1
    assert listed["items"][0]["last_capture_at"] is None


def test_a_saved_view_stores_what_the_filter_bar_produced(authed: TestClient) -> None:
    """The end-to-end version of the round trip: what goes in comes back as a
    query string the sites endpoint accepts unchanged."""
    _site(authed, "match.example.com", tags=["travel"])
    _site(authed, "other.example.com")

    view = authed.post(
        "/api/views",
        json={"name": "Travel", "query": {"tags": ["travel"], "sort": "title"}},
        headers=XHR,
    )
    assert view.status_code == 201, view.text
    stored = view.json()

    assert stored["query"] == {"tags": ["travel"], "sort": "title"}
    replayed = authed.get(f"/api/sites?{stored['query_string']}", headers=XHR).json()
    assert replayed["total"] == 1
    assert replayed["items"][0]["seed_url"].startswith("https://match.")


def test_a_saved_view_with_an_unusable_query_is_refused_when_saved(
    authed: TestClient,
) -> None:
    bad = authed.post(
        "/api/views", json={"name": "Broken", "query": {"sort": "rowid"}}, headers=XHR
    )

    assert bad.status_code == 400
    assert "sort" in bad.json()["error"]["message"]


def test_an_unreadable_filter_is_a_400_not_a_500(authed: TestClient) -> None:
    response = authed.get("/api/sites?size_min=lots", headers=XHR)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_filter"
