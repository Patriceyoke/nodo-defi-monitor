"""The first https link in a note becomes the Paga button. Nothing else is markup."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))

_TMP = Path(tempfile.mkdtemp(prefix="casa-paga-"))
os.environ["DATA_DIR"] = str(_TMP)

import logic  # noqa: E402
import server  # noqa: E402

logic.DATA_DIR = _TMP
logic.DB_PATH = _TMP / "casa.db"
logic.TOKEN_PATH = _TMP / "agent.token"
logic.ensure_storage()

PAY = "https://example.com/pay?q=1"


def insert(external_id: str, section: str, note: str) -> str:
    payload = {
        "title": "Voce",
        "titolo": "Voce",
        "person": "",
        "due_day": 10,
        "due_date": "",
        "interval": "monthly",
        "giorno": 10,
        "notes": note,
        "note": note,
        "status": "pending",
        "section": section,
    }
    if section == "documenti":
        payload["tipo"] = "Voce"
        payload["scadenza"] = "2027-06-01"
        payload["due_date"] = "2027-06-01"
        payload["interval"] = "yearly"
        payload["due_day"] = None
    conn = sqlite3.connect(logic.DB_PATH)
    try:
        conn.execute(
            "INSERT INTO items (id, external_id, section, payload, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (external_id, external_id, section, json.dumps(payload), "2026-01-01T00:00:00+07:00", "2026-01-01T00:00:00+07:00"),
        )
        conn.commit()
    finally:
        conn.close()
    return external_id


def test_first_https_only_and_escaped():
    assert server.first_https("prima http://example.com no") == ""
    assert server.first_https(f"testo {PAY} dopo https://second.example/a.") == PAY
    raw = f'Prima <b>x</b> {PAY}" onclick=alert(1) dopo https://second.example/a'
    page = None
    item_id = insert("pay-1", "casa", raw)
    view = logic.get_item(item_id)
    html = server.page_item(view)
    assert html.count(">Paga</a>") == 1
    assert f'href="{PAY}"' in html
    assert 'target="_blank"' in html
    assert 'rel="noopener noreferrer"' in html
    assert html.index(">Paga</a>") < html.index("Segna pagato")
    start = html.index('<a class="button" href="')
    tag = html[start:html.index(">Paga</a>") + len(">Paga</a>")]
    assert tag.startswith(f'<a class="button" href="{PAY}"')
    assert "onclick" not in tag
    assert "<b>" not in html
    assert "<script" not in html
    assert "&lt;b&gt;" in html
    assert "https://second.example/a" in html
    assert "dopo" in html
    plain = insert("plain-1", "garage", "solo testo")
    plain_html = server.page_item(logic.get_item(plain))
    assert ">Paga</a>" not in plain_html
    doc = insert("doc-1", "documenti", f"Vedi {PAY}")
    doc_html = server.page_item(logic.get_item(doc))
    assert ">Paga</a>" in doc_html
    assert "Segna fatto" in doc_html
    assert doc_html.index(">Paga</a>") < doc_html.index("Segna fatto")


def test_upgrade_from_103_keeps_rows():
    cols = [row[1] for row in sqlite3.connect(logic.DB_PATH).execute("PRAGMA table_info(items)")]
    insert("keep-103", "casa", "nessun link")
    logic.ensure_storage()
    conn = sqlite3.connect(logic.DB_PATH)
    try:
        still = conn.execute("SELECT 1 FROM items WHERE external_id = ?", ("keep-103",)).fetchone()
        assert still is not None
        assert [row[1] for row in conn.execute("PRAGMA table_info(items)")] == cols
        conn.execute("DELETE FROM history")
        conn.execute("DELETE FROM items")
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0
    finally:
        conn.close()
