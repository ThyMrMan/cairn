"""Shared fixtures.

Each test gets a fresh temp directory, a fresh database and a fresh app, so
nothing leaks between tests — including the rate-limit ledger, which is
persistent by design and would otherwise lock out later tests.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from cairn.config import Settings
from cairn.crypto.sealing import Sealer

TEST_KEY = "test-master-key-must-be-at-least-32-bytes-long"
USERNAME = "admin"
PASSWORD = "correct-horse-battery-staple"
XHR = {"X-Requested-With": "XMLHttpRequest"}


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop a developer's .env from leaking into tests."""
    for key in list(os.environ):
        if key.startswith("CAIRN_"):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        config_dir=tmp_path / "config",
        data_dir=tmp_path / "data",
        secret_key=TEST_KEY,
        log_json=False,
        log_level="WARNING",
        _env_file=None,  # type: ignore[call-arg]
    )


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    from cairn.app import create_app

    return create_app(settings)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


@pytest.fixture
def db(client: TestClient) -> Iterator[Session]:
    """A session against the same database the client uses."""
    factory = client.app.state.sessionmaker  # type: ignore[attr-defined]
    with factory() as session:
        yield session


@pytest.fixture
def sealer() -> Sealer:
    return Sealer(TEST_KEY.encode())


@pytest.fixture
def authed(client: TestClient) -> TestClient:
    """A client that has completed setup and is logged in."""
    res = client.post("/api/setup", json={"username": USERNAME, "password": PASSWORD}, headers=XHR)
    assert res.status_code == 201, res.text
    return client
