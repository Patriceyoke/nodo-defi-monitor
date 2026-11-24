"""One temporary database per test module. Restores the previous path afterwards."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[1] / "app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))


@pytest.fixture(scope="module")
def casa(tmp_path_factory):
    import logic
    import server

    previous = (logic.DATA_DIR, logic.DB_PATH, logic.TOKEN_PATH, os.environ.get("DATA_DIR"))
    folder = tmp_path_factory.mktemp("casa")
    os.environ["DATA_DIR"] = str(folder)
    logic.DATA_DIR = folder
    logic.DB_PATH = folder / "casa.db"
    logic.TOKEN_PATH = folder / "agent.token"
    logic.ensure_storage()
    yield logic, server
    logic.DATA_DIR, logic.DB_PATH, logic.TOKEN_PATH = previous[:3]
    if previous[3] is None:
        os.environ.pop("DATA_DIR", None)
    else:
        os.environ["DATA_DIR"] = previous[3]
