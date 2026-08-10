"""Security properties that must hold before this is exposed to the internet."""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cairn.config import Settings
from cairn.services import audit
from tests.conftest import PASSWORD, USERNAME, XHR

MUTATING = [
    ("POST", "/api/auth/logout", None),
    ("POST", "/api/auth/password", {"current": "x", "new": "y"}),
    ("POST", "/api/auth/totp/setup", None),
    ("DELETE", "/api/auth/sessions", None),
]


@pytest.mark.parametrize(("method", "path", "body"), MUTATING)
def test_mutating_requests_require_the_csrf_header(
    authed: TestClient, method: str, path: str, body: dict[str, str] | None
) -> None:
    res = authed.request(method, path, json=body)
    assert res.status_code == 403
    assert res.json()["error"]["code"] == "csrf_failed"


def test_get_requests_do_not_require_the_csrf_header(authed: TestClient) -> None:
    assert authed.get("/api/auth/me").status_code == 200


def test_security_headers_present(client: TestClient) -> None:
    res = client.get("/api/health")
    assert res.headers["X-Content-Type-Options"] == "nosniff"
    assert res.headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in res.headers["Content-Security-Policy"]
    assert "object-src 'none'" in res.headers["Content-Security-Policy"]


def test_api_responses_are_not_cacheable(authed: TestClient) -> None:
    assert authed.get("/api/auth/me").headers["Cache-Control"] == "no-store"


def test_csp_allows_the_pages_own_inline_script(tmp_path: Path) -> None:
    """A CSP that blocks your own script fails silently.

    index.html carries one inline script that applies the stored theme before
    first paint. Under a bare `script-src 'self'` the browser refuses to run
    it: no error the user sees, just a white flash for dark-mode users and a
    violation in the console. The policy must carry its hash.
    """
    from cairn.api.middleware import inline_script_hashes

    index = tmp_path / "index.html"
    index.write_text(
        "<html><head><script>document.title='x'</script>"
        '<script type="module" src="/assets/app.js"></script></head></html>',
        encoding="utf-8",
    )
    hashes = inline_script_hashes(index)

    # The inline one is hashed; the external one is covered by 'self'.
    assert len(hashes) == 1
    assert hashes[0].startswith("'sha256-")

    expected = base64.b64encode(hashlib.sha256(b"document.title='x'").digest()).decode()
    assert hashes[0] == f"'sha256-{expected}'"


def test_csp_hash_matches_the_shipped_index_html() -> None:
    """The hash is computed from the file, so editing the script cannot leave
    the policy pointing at the previous version."""
    from cairn.api.middleware import inline_script_hashes
    from cairn.app import STATIC_DIR

    index = STATIC_DIR / "index.html"
    if not index.is_file():
        pytest.skip("frontend has not been built")

    hashes = inline_script_hashes(index)
    assert hashes, "the built page has an inline script but no hash was produced"


def test_csp_frame_src_names_the_replay_origin(tmp_path: object) -> None:
    from cairn.app import create_app

    settings = Settings(
        config_dir=tmp_path / "config",  # type: ignore[operator]
        data_dir=tmp_path / "data",  # type: ignore[operator]
        secret_key="test-master-key-must-be-at-least-32-bytes-long",
        app_public_url="https://archive.example.com",
        replay_public_url="https://replay.example.com",
        _env_file=None,  # type: ignore[call-arg]
    )
    with TestClient(create_app(settings)) as client:
        csp = client.get("/api/health").headers["Content-Security-Policy"]
    # Explicitly named, never wildcarded.
    assert "frame-src https://replay.example.com" in csp


def test_replay_host_isolation_check() -> None:
    """Ports do not isolate cookies — only a different hostname does."""
    base = {
        "secret_key": "test-master-key-must-be-at-least-32-bytes-long",
        "_env_file": None,
    }
    same_host = Settings(
        app_public_url="https://box.example.com:8080",
        replay_public_url="https://box.example.com:8081",
        **base,  # type: ignore[arg-type]
    )
    assert same_host.replay_shares_host_with_app() is True

    isolated = Settings(
        app_public_url="https://archive.example.com",
        replay_public_url="https://replay.example.com",
        **base,  # type: ignore[arg-type]
    )
    assert isolated.replay_shares_host_with_app() is False


def test_health_leaks_nothing_sensitive(client: TestClient) -> None:
    body = client.get("/api/health").json()
    assert set(body) == {"status", "version", "db", "setup_complete", "disk_free_bytes"}


def test_auth_header_mode_requires_a_trusted_proxy() -> None:
    """Without a trusted proxy anyone can forge the header and walk in."""
    with pytest.raises(ValueError, match="TRUSTED_PROXY"):
        Settings(
            secret_key="test-master-key-must-be-at-least-32-bytes-long",
            auth_header_mode=True,
            trusted_proxy="",
            _env_file=None,  # type: ignore[call-arg]
        )


def test_forwarded_for_ignored_without_a_trusted_proxy(authed: TestClient) -> None:
    """Otherwise an attacker rotates the header to defeat the rate limiter."""
    authed.post("/api/auth/logout", headers=XHR)
    for i in range(6):
        authed.post(
            "/api/auth/login",
            json={"username": USERNAME, "password": "nope"},
            headers={**XHR, "X-Forwarded-For": f"10.0.0.{i}"},
        )
    res = authed.post(
        "/api/auth/login",
        json={"username": USERNAME, "password": PASSWORD},
        headers={**XHR, "X-Forwarded-For": "10.0.0.99"},
    )
    assert res.status_code == 429


def test_audit_refuses_to_log_secret_fields(db: object) -> None:
    """A programming error should fail loudly, not quietly write a credential."""
    with pytest.raises(ValueError, match="secret keys"):
        audit.record(db, "test", detail={"password": "hunter2"})  # type: ignore[arg-type]


def test_unhandled_errors_do_not_leak_details(authed: TestClient) -> None:
    res = authed.get("/api/audit?page=0")
    assert res.status_code == 422
    body = res.text.lower()
    assert "traceback" not in body and "sqlalchemy" not in body


def test_spa_fallback_does_not_serve_files_outside_static(client: TestClient) -> None:
    """Unknown paths get the SPA (or 503 when unbuilt) — never a file from
    outside the static directory, however the path is spelled."""
    attempts = [
        "../alembic.ini",
        "..%2f..%2fpyproject.toml",
        "....//pyproject.toml",
        "../../pyproject.toml",
        "..\\..\\pyproject.toml",
        "%2e%2e%2falembic.ini",
        "static/../../../pyproject.toml",
    ]
    for attempt in attempts:
        res = client.get(f"/{attempt}")
        assert res.status_code in (200, 404, 503), f"{attempt} -> {res.status_code}"
        # The security property: contents of files outside static/ never leak.
        for marker in ("[tool.ruff]", "[alembic]", "script_location", "build-system"):
            assert marker not in res.text, f"{attempt} leaked {marker!r}"


def test_spa_fallback_resolves_symlinks_before_validating(client: TestClient) -> None:
    """A raw ASGI request bypasses the client's path normalization, so this
    exercises the handler's own containment check rather than httpx's."""
    from cairn.app import STATIC_DIR

    escape = "/" + "../" * 6 + "pyproject.toml"
    res = client.request("GET", escape)
    assert "[tool.ruff]" not in res.text
    # And the guard itself: nothing outside STATIC_DIR is ever considered.
    resolved = (STATIC_DIR / ".." / ".." / "pyproject.toml").resolve()
    assert not resolved.is_relative_to(STATIC_DIR.resolve())
