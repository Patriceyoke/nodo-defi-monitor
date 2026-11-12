
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

# Isolate SQLite before importing the app.
_TMP = Path(tempfile.mkdtemp(prefix="monitor-defi-test-"))
os.environ["DATA_DIR"] = str(_TMP)

from app import db  # noqa: E402
from app.main import app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture(scope="session")
def data_dir() -> Path:
    return _TMP


@pytest.fixture
def client() -> TestClient:
    db.init_db()
    return TestClient(app)
