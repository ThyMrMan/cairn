"""Setup, login, sessions, TOTP."""

from __future__ import annotations

import pyotp
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete
from sqlalchemy.orm import Session

from cairn.db.models import LoginAttempt, Session_, User
from tests.conftest import PASSWORD, USERNAME, XHR

# ── setup ────────────────────────────────────────────────────────────────


def test_setup_status_before_and_after(client: TestClient) -> None:
    assert client.get("/api/setup").json()["setup_complete"] is False
    client.post("/api/setup", json={"username": USERNAME, "password": PASSWORD}, headers=XHR)
    assert client.get("/api/setup").json()["setup_complete"] is True


def test_setup_creates_account_and_logs_in(client: TestClient) -> None:
    res = client.post("/api/setup", json={"username": USERNAME, "password": PASSWORD}, headers=XHR)
    assert res.status_code == 201
    assert client.get("/api/auth/me").json()["username"] == USERNAME


def test_setup_rejects_weak_password(client: TestClient) -> None:
    res = client.post("/api/setup", json={"username": USERNAME, "password": "short"}, headers=XHR)
    assert res.status_code == 400
    assert res.json()["error"]["detail"], "should explain what is wrong"


def test_setup_is_unreachable_once_a_user_exists(authed: TestClient) -> None:
    """The critical guard: server-side, not just hidden in the UI."""
    res = authed.post(
        "/api/setup", json={"username": "intruder", "password": PASSWORD}, headers=XHR
    )
    assert res.status_code == 409


def test_setup_locked_out_even_when_unauthenticated(client: TestClient) -> None:
    client.post("/api/setup", json={"username": USERNAME, "password": PASSWORD}, headers=XHR)
    client.cookies.clear()
    res = client.post(
        "/api/setup", json={"username": "intruder", "password": PASSWORD}, headers=XHR
    )
    assert res.status_code == 409


# ── login ────────────────────────────────────────────────────────────────


def test_login_logout_cycle(authed: TestClient) -> None:
    assert authed.post("/api/auth/logout", headers=XHR).status_code == 200
    assert authed.get("/api/auth/me").status_code == 401

    res = authed.post(
        "/api/auth/login", json={"username": USERNAME, "password": PASSWORD}, headers=XHR
    )
    assert res.status_code == 200
    assert authed.get("/api/auth/me").status_code == 200


def test_wrong_password_and_unknown_user_are_indistinguishable(authed: TestClient) -> None:
    authed.post("/api/auth/logout", headers=XHR)
    wrong = authed.post(
        "/api/auth/login", json={"username": USERNAME, "password": "nope"}, headers=XHR
    )
    unknown = authed.post(
        "/api/auth/login", json={"username": "ghost", "password": "nope"}, headers=XHR
    )
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json()["error"] == unknown.json()["error"], "account enumeration"


def test_session_cookie_flags(authed: TestClient) -> None:
    authed.post("/api/auth/logout", headers=XHR)
    res = authed.post(
        "/api/auth/login", json={"username": USERNAME, "password": PASSWORD}, headers=XHR
    )
    raw = "; ".join(res.headers.get_list("set-cookie"))
    assert "HttpOnly" in raw
    assert "samesite=lax" in raw.lower()  # Starlette emits it lowercase
    # Domain must never be set: a host-only cookie cannot reach the replay
    # origin, where archived JavaScript runs.
    assert "Domain=" not in raw


def test_session_token_is_not_stored_verbatim(authed: TestClient, db: Session) -> None:
    token = authed.cookies.get("cairn_session")
    assert token
    stored = db.query(Session_).all()
    assert stored and all(row.id != token for row in stored)


def test_logout_revokes_the_token(authed: TestClient, db: Session) -> None:
    token = authed.cookies.get("cairn_session")
    authed.post("/api/auth/logout", headers=XHR)
    # Even replaying the raw cookie must fail.
    authed.cookies.set("cairn_session", token or "")
    assert authed.get("/api/auth/me").status_code == 401


# ── rate limiting ────────────────────────────────────────────────────────


def test_rate_limit_after_repeated_failures(authed: TestClient) -> None:
    authed.post("/api/auth/logout", headers=XHR)
    for _ in range(5):
        authed.post("/api/auth/login", json={"username": USERNAME, "password": "nope"}, headers=XHR)
    res = authed.post(
        "/api/auth/login", json={"username": USERNAME, "password": PASSWORD}, headers=XHR
    )
    assert res.status_code == 429, "a correct password must not bypass the limiter"
    assert "retry-after" in {k.lower() for k in res.headers}


def test_failed_attempts_persist_for_the_limiter(authed: TestClient, db: Session) -> None:
    """The request errors, but the ledger row must still be committed —
    otherwise the limiter has nothing to count."""
    authed.post("/api/auth/logout", headers=XHR)
    authed.post("/api/auth/login", json={"username": USERNAME, "password": "nope"}, headers=XHR)
    assert db.query(LoginAttempt).filter_by(successful=False).count() == 1


def test_login_works_again_once_attempts_age_out(authed: TestClient, db: Session) -> None:
    authed.post("/api/auth/logout", headers=XHR)
    for _ in range(6):
        authed.post("/api/auth/login", json={"username": USERNAME, "password": "nope"}, headers=XHR)

    db.execute(delete(LoginAttempt))
    user = db.query(User).one()
    user.locked_until = None
    user.failed_logins = 0
    db.commit()

    res = authed.post(
        "/api/auth/login", json={"username": USERNAME, "password": PASSWORD}, headers=XHR
    )
    assert res.status_code == 200


# ── TOTP ─────────────────────────────────────────────────────────────────


def _enable_totp(client: TestClient) -> tuple[str, list[str]]:
    secret = client.post("/api/auth/totp/setup", headers=XHR).json()["secret"]
    res = client.post(
        "/api/auth/totp/confirm", json={"code": pyotp.TOTP(secret).now()}, headers=XHR
    )
    assert res.status_code == 200, res.text
    return secret, res.json()["recovery_codes"]


def test_totp_enable_and_login(authed: TestClient) -> None:
    secret, _ = _enable_totp(authed)
    authed.post("/api/auth/logout", headers=XHR)

    res = authed.post(
        "/api/auth/login", json={"username": USERNAME, "password": PASSWORD}, headers=XHR
    )
    assert res.status_code == 401
    assert res.json()["error"]["code"] == "totp_required"

    res = authed.post(
        "/api/auth/login",
        json={"username": USERNAME, "password": PASSWORD, "totp": pyotp.TOTP(secret).now()},
        headers=XHR,
    )
    assert res.status_code == 200


def test_totp_secret_is_sealed_at_rest(authed: TestClient, db: Session) -> None:
    secret, _ = _enable_totp(authed)
    stored = db.query(User).one().totp_secret
    assert stored is not None
    assert secret.encode() not in stored


def test_recovery_code_works_once(authed: TestClient) -> None:
    _, codes = _enable_totp(authed)
    code = codes[0]
    authed.post("/api/auth/logout", headers=XHR)

    first = authed.post(
        "/api/auth/login",
        json={"username": USERNAME, "password": PASSWORD, "totp": code},
        headers=XHR,
    )
    assert first.status_code == 200

    authed.post("/api/auth/logout", headers=XHR)
    second = authed.post(
        "/api/auth/login",
        json={"username": USERNAME, "password": PASSWORD, "totp": code},
        headers=XHR,
    )
    assert second.status_code == 401, "a recovery code must not be reusable"


def test_totp_disable_requires_password_and_code(authed: TestClient) -> None:
    secret, _ = _enable_totp(authed)
    bad = authed.request(
        "DELETE",
        "/api/auth/totp",
        json={"password": "wrong", "code": pyotp.TOTP(secret).now()},
        headers=XHR,
    )
    assert bad.status_code == 401

    good = authed.request(
        "DELETE",
        "/api/auth/totp",
        json={"password": PASSWORD, "code": pyotp.TOTP(secret).now()},
        headers=XHR,
    )
    assert good.status_code == 200
    assert authed.get("/api/auth/me").json()["totp_enabled"] is False


# ── password change ──────────────────────────────────────────────────────


def test_password_change_revokes_other_sessions(authed: TestClient) -> None:
    other = TestClient(authed.app)
    other.post("/api/auth/login", json={"username": USERNAME, "password": PASSWORD}, headers=XHR)
    assert other.get("/api/auth/me").status_code == 200

    new_password = "another-long-passphrase-here"
    res = authed.post(
        "/api/auth/password", json={"current": PASSWORD, "new": new_password}, headers=XHR
    )
    assert res.status_code == 200
    assert res.json()["revoked_sessions"] >= 1

    assert other.get("/api/auth/me").status_code == 401, "other sessions must be signed out"
    assert authed.get("/api/auth/me").status_code == 200, "current session should survive"


def test_password_change_requires_current_password(authed: TestClient) -> None:
    res = authed.post(
        "/api/auth/password",
        json={"current": "wrong", "new": "another-long-passphrase"},
        headers=XHR,
    )
    assert res.status_code == 401


def test_password_change_rejects_weak_new_password(authed: TestClient) -> None:
    res = authed.post("/api/auth/password", json={"current": PASSWORD, "new": "short"}, headers=XHR)
    assert res.status_code == 400


# ── session management ───────────────────────────────────────────────────


def test_session_list_marks_exactly_one_current(authed: TestClient) -> None:
    rows = authed.get("/api/auth/sessions").json()
    assert sum(1 for r in rows if r["current"]) == 1


def test_revoke_other_sessions(authed: TestClient) -> None:
    other = TestClient(authed.app)
    other.post("/api/auth/login", json={"username": USERNAME, "password": PASSWORD}, headers=XHR)

    assert authed.request("DELETE", "/api/auth/sessions", headers=XHR).status_code == 200
    assert other.get("/api/auth/me").status_code == 401
    assert authed.get("/api/auth/me").status_code == 200


@pytest.mark.parametrize("path", ["/api/auth/me", "/api/auth/sessions", "/api/audit"])
def test_endpoints_require_authentication(client: TestClient, path: str) -> None:
    client.post("/api/setup", json={"username": USERNAME, "password": PASSWORD}, headers=XHR)
    client.cookies.clear()
    assert client.get(path).status_code == 401
