"""DELETE /api/items/{external_id} and the one probe-row cleanup."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from io import BytesIO
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))

_TMP = Path(tempfile.mkdtemp(prefix="casa-delete-"))
os.environ["DATA_DIR"] = str(_TMP)

import logic  # noqa: E402
import server  # noqa: E402

logic.DATA_DIR = _TMP
logic.DB_PATH = _TMP / "casa.db"
logic.TOKEN_PATH = _TMP / "agent.token"
logic.ensure_storage()

EM = "\u2014"


class Headers(dict):
    def get(self, key, default=""):
        return super().get(key, default)


def call(method: str, path: str, ip: str, bearer: str | None = None, extra: dict | None = None):
    handler = server.Handler.__new__(server.Handler)
    handler.client_address = (ip, 40000)
    handler.headers = Headers()
    if bearer is not None:
        handler.headers["Authorization"] = bearer
    if extra:
        handler.headers.update(extra)
    handler.rfile = BytesIO()
    handler.wfile = BytesIO()
    handler.path = path
    handler.command = method
    handler.requestline = f"{method} {path} HTTP/1.1"
    handler.request_version = "HTTP/1.1"
    handler.close_connection = True
    if method == "DELETE":
        handler.do_DELETE()
    else:
        handler.do_POST()
    text = handler.wfile.getvalue().decode()
    status = int(text.split(" ", 2)[1])
    payload = {}
    if "\r\n\r\n" in text and text.split("\r\n\r\n", 1)[1]:
        payload = json.loads(text.split("\r\n\r\n", 1)[1])
    return status, payload


def columns(table: str) -> list[str]:
    conn = sqlite3.connect(logic.DB_PATH)
    try:
        return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
    finally:
        conn.close()


def insert(external_id: str, payload: dict, history: bool = False) -> str:
    conn = sqlite3.connect(logic.DB_PATH)
    try:
        conn.execute(
            "INSERT INTO items (id, external_id, section, payload, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (external_id, external_id, "casa", json.dumps(payload), "2026-01-01T00:00:00+07:00", "2026-01-01T00:00:00+07:00"),
        )
        if history:
            conn.execute(
                "INSERT INTO history (item_id, kind, period, paid_at, note, amount_thb, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (external_id, "pagato", "2026-01", "2026-01-01", "", None, "2026-01-01T00:00:00+07:00"),
            )
        conn.commit()
    finally:
        conn.close()
    return external_id


def history_count(item_id: str) -> int:
    conn = sqlite3.connect(logic.DB_PATH)
    try:
        return conn.execute("SELECT COUNT(*) FROM history WHERE item_id = ?", (item_id,)).fetchone()[0]
    finally:
        conn.close()


def exists(external_id: str) -> bool:
    conn = sqlite3.connect(logic.DB_PATH)
    try:
        return conn.execute("SELECT 1 FROM items WHERE external_id = ?", (external_id,)).fetchone() is not None
    finally:
        conn.close()


def test_delete_auth_and_history():
    insert("to-delete", {"title": "Voce", "due_date": "2026-12-01", "interval": "once"}, history=True)
    assert history_count("to-delete") == 1
    status, _ = call("DELETE", "/api/items/to-delete", "203.0.113.4", None, {"X-Forwarded-For": "10.21.0.15"})
    assert status == 401
    assert exists("to-delete")
    status, _ = call("DELETE", "/api/items/to-delete", "10.21.0.15", "Bearer sbagliato")
    assert status == 401
    assert exists("to-delete")
    status, body = call("DELETE", "/api/items/to-delete", "10.21.0.15")
    assert status == 200 and body == {"ok": True}
    assert not exists("to-delete")
    assert history_count("to-delete") == 0
    status, _ = call("DELETE", "/api/items/to-delete", "172.17.0.3")
    assert status == 404
    token = logic.TOKEN_PATH.read_text(encoding="ascii").strip()
    insert("outside", {"title": "Altra", "due_date": "2026-12-02", "interval": "once"}, history=True)
    status, _ = call("DELETE", "/api/items/outside", "198.51.100.9", f"Bearer {token}")
    assert status == 200
    assert not exists("outside")


def test_upgrade_from_102_keeps_data_and_drops_only_the_probe():
    item_cols = columns("items")
    history_cols = columns("history")
    insert(
        "keep-me",
        {"title": "Bolletta", "due_day": 10, "due_date": "", "interval": "monthly", "status": "pending"},
        history=True,
    )
    insert(
        "lookalike",
        {"title": EM, "due_date": "2099-01-01", "interval": "once", "status": "pending"},
        history=True,
    )
    insert(
        "probe-schema",
        {"title": "altro", "due_date": "2099-01-01", "interval": "once", "status": "pending"},
        history=True,
    )
    logic.ensure_storage()
    assert exists("probe-schema")
    assert exists("lookalike")
    assert exists("keep-me")
    assert history_count("keep-me") == 1
    assert columns("items") == item_cols
    assert columns("history") == history_cols

    conn = sqlite3.connect(logic.DB_PATH)
    conn.execute(
        "UPDATE items SET payload = ? WHERE external_id = ?",
        (json.dumps({"title": EM, "due_date": "2099-01-02", "interval": "once"}), "probe-schema"),
    )
    conn.commit()
    conn.close()
    logic.ensure_storage()
    assert exists("probe-schema")

    conn = sqlite3.connect(logic.DB_PATH)
    conn.execute(
        "UPDATE items SET payload = ? WHERE external_id = ?",
        (json.dumps({"titolo": EM, "scadenza": "2099-01-01"}), "probe-schema"),
    )
    conn.commit()
    conn.close()
    logic.ensure_storage()
    assert not exists("probe-schema")
    assert history_count("probe-schema") == 0
    assert exists("lookalike")
    assert exists("keep-me")
    assert history_count("keep-me") == 1
    assert history_count("lookalike") == 1
    logic.ensure_storage()
    assert exists("keep-me")
    assert exists("lookalike")
    assert columns("items") == item_cols
