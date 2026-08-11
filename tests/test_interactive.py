"""The interactive profile session.

The handshake tests need no browser and are the important ones: a WebSocket
obeys none of the protections the rest of the API relies on, and this socket
carries a live, driveable view of whatever the user is signed into.

The session tests drive a real Chromium and run in the container.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from cairn.services import browser
from tests.conftest import XHR

needs_browser = pytest.mark.skipif(
    not browser.availability()[0], reason="needs Playwright and Chromium"
)


def make_profile(client: TestClient, verify_url: str = "https://example.com/") -> int:
    created = client.post(
        "/api/profiles",
        json={"name": "interactive-test", "mode": "interactive", "verify_url": verify_url},
        headers=XHR,
    )
    assert created.status_code == 201, created.text
    profile_id: int = created.json()["id"]
    return profile_id


# ── the handshake ────────────────────────────────────────────────────────


def test_the_socket_refuses_a_cross_origin_handshake(authed: TestClient) -> None:
    """The whole reason this router checks Origin itself.

    The same-origin policy does not apply to WebSockets, and the handshake
    carries no CSRF header to check — so any page on the internet can open a
    socket to a LAN address and the browser will attach the session cookie.
    What it would get here is a live browser it can drive.
    """
    profile_id = make_profile(authed)

    with (
        pytest.raises(WebSocketDisconnect) as caught,
        authed.websocket_connect(
            f"/api/profiles/{profile_id}/interactive/ws?session_id=whatever",
            headers={"Origin": "https://attacker.example"},
        ),
    ):
        pass

    assert caught.value.code == 1008


def test_the_socket_refuses_an_unauthenticated_connection(client: TestClient) -> None:
    with (
        pytest.raises(WebSocketDisconnect) as caught,
        client.websocket_connect("/api/profiles/1/interactive/ws?session_id=x"),
    ):
        pass

    assert caught.value.code == 1008


def test_the_socket_refuses_an_unknown_session(authed: TestClient) -> None:
    profile_id = make_profile(authed)

    with (
        pytest.raises(WebSocketDisconnect) as caught,
        authed.websocket_connect(
            f"/api/profiles/{profile_id}/interactive/ws?session_id=not-a-real-session"
        ),
    ):
        pass

    assert caught.value.code == 1008


def test_the_csp_lets_the_page_open_its_own_websocket(authed: TestClient) -> None:
    """The interactive pane is a canvas fed entirely by that socket, so a CSP
    that blocks it shows an empty box and explains itself only in the console —
    exactly how the replay tab failed in M3."""
    policy = authed.get("/api/health").headers["Content-Security-Policy"]

    connect = next(d for d in policy.split(";") if d.strip().startswith("connect-src"))
    assert "ws://" in connect or "wss://" in connect, connect


# ── saving ───────────────────────────────────────────────────────────────


def test_saving_without_a_session_is_refused(authed: TestClient) -> None:
    profile_id = make_profile(authed)

    response = authed.post(f"/api/profiles/{profile_id}/interactive/save", headers=XHR)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "no_session"


@needs_browser
def test_a_session_starts_streams_and_saves_what_it_collected(
    authed: TestClient, gated_server: str
) -> None:
    """The second half of M5's exit criterion, minus the human.

    A person would click the button; the test drives the same input path the
    UI uses — a mouse press and release at a coordinate — and then saves,
    which is the part that has to produce a usable jar.
    """
    import json

    from tests.conftest import INTERSTITIAL_BUTTON

    profile_id = make_profile(authed, verify_url=gated_server)
    started = authed.post(
        f"/api/profiles/{profile_id}/interactive", json={"url": gated_server}, headers=XHR
    )
    assert started.status_code == 200, started.text
    session_id = started.json()["session_id"]
    x, y = INTERSTITIAL_BUTTON

    try:
        with authed.websocket_connect(
            f"/api/profiles/{profile_id}/interactive/ws?session_id={session_id}"
        ) as socket:
            assert socket.receive()["type"] == "websocket.send"

            for action in ("down", "up"):
                socket.send_json(
                    {"type": "mouse", "action": action, "x": x, "y": y, "clickCount": 1}
                )

            # Accepting navigates to a different path, so the URL is what says
            # the click landed — pixels cannot be asserted on.
            landed = False
            for _ in range(40):
                socket.send_json({"type": "where"})
                message = socket.receive()
                text = message.get("text")
                if text and "post-1.html" in json.loads(text).get("url", ""):
                    landed = True
                    break
            assert landed, "the click never reached the page"

        saved = authed.post(f"/api/profiles/{profile_id}/interactive/save", headers=XHR)
        assert saved.status_code == 200, saved.text
        body = saved.json()
        assert body["cookie_count"] >= 1
        assert body["profile"]["has_storage"], "the full storage state should be kept too"
        assert body["profile"]["mode"] == "interactive"
    finally:
        authed.delete(f"/api/profiles/{profile_id}/interactive", headers=XHR)


@needs_browser
def test_only_one_session_runs_at_a_time(authed: TestClient, gated_server: str) -> None:
    """A browser is hundreds of megabytes and the thing driving it is a
    person. The cap is what stops a forgotten tab holding one open."""
    profile_id = make_profile(authed, verify_url=gated_server)
    first = authed.post(
        f"/api/profiles/{profile_id}/interactive", json={"url": gated_server}, headers=XHR
    )
    assert first.status_code == 200, first.text

    try:
        second = authed.post(
            f"/api/profiles/{profile_id}/interactive", json={"url": gated_server}, headers=XHR
        )
        assert second.status_code == 409
        assert second.json()["error"]["code"] == "session_exists"
    finally:
        authed.delete(f"/api/profiles/{profile_id}/interactive", headers=XHR)
