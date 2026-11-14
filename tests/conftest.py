"""Pytest fixtures: isolated SQLite database + API client per test session.

DATABASE_URL points at a throwaway SQLite file created before the app package
is imported.  Each test uses uniquely-coded fields so tests stay independent.
"""
from __future__ import annotations

import os
import pathlib
import sys
import uuid

# Make the in-repo app package importable without installation.
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_DB_PATH = pathlib.Path("/tmp") / f"qc-test-{uuid.uuid4().hex}.db"
os.environ["DATABASE_URL"] = f"sqlite+pysqlite:///{_DB_PATH}"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture(scope="session")
def client():
    from app.db import init_db
    from app.main import app

    init_db()
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def field(client):
    code = f"F-{uuid.uuid4().hex[:8]}"
    r = client.post("/api/v1/fields", json={"code": code, "name": "测试田块"})
    assert r.status_code == 200, r.text
    return r.json()
