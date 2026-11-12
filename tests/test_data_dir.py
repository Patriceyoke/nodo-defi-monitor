"""DATA_DIR must stay usable when Umbrel's bind mount is owned by root."""
from __future__ import annotations

import os
import sqlite3

import pytest

from app import config, db, main


def test_writable_directory_is_kept(tmp_path):
    chosen, problem = config.select_writable_dir(tmp_path)
    assert problem is None
    assert chosen == tmp_path


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write inside a mode-555 directory")
def test_unwritable_directory_falls_back_to_temp(tmp_path):
    blocked = tmp_path / "data"
    blocked.mkdir()
    blocked.chmod(0o555)
    chosen, problem = config.select_writable_dir(blocked)
    assert problem
    assert "not writable" in problem
    assert chosen != blocked
    assert chosen.is_dir()
    assert os.access(chosen, os.W_OK)
    probe = chosen / "monitor.db"
    conn = sqlite3.connect(probe)
    conn.execute("CREATE TABLE t(x)")
    conn.commit()
    conn.close()
    assert not (blocked / "monitor.db").exists()


def test_startup_does_not_crash_when_database_init_fails(monkeypatch):
    def boom() -> None:
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(db, "init_db", boom)
    main._startup()


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write inside a mode-555 directory")
def test_open_connection_switches_away_from_root_owned_dir(tmp_path):
    saved = (config.DATA_DIR, config.DB_PATH, config.DATA_PERSISTENT, config._READY)
    blocked = tmp_path / "root-data"
    blocked.mkdir()
    blocked.chmod(0o555)
    config.DATA_DIR = blocked
    config.DB_PATH = blocked / "monitor.db"
    config.DATA_PERSISTENT = True
    config._READY = True
    try:
        db.init_db()
        assert config.DATA_PERSISTENT is False
        assert config.DB_PATH.parent != blocked
        assert config.DB_PATH.exists()
        assert not (blocked / "monitor.db").exists()
        assert db.get_setting("mcp_token")
    finally:
        config.DATA_DIR, config.DB_PATH, config.DATA_PERSISTENT, config._READY = saved
        db.init_db()
