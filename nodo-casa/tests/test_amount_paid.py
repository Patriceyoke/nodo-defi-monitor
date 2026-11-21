"""Amount next to the date, and this month's paid rows in a closed block."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))

_TMP = Path(tempfile.mkdtemp(prefix="casa-amount-"))
os.environ["DATA_DIR"] = str(_TMP)

import logic  # noqa: E402
import server  # noqa: E402

logic.DATA_DIR = _TMP
logic.DB_PATH = _TMP / "casa.db"
logic.TOKEN_PATH = _TMP / "agent.token"
logic.ensure_storage()


def insert(external_id: str, title: str, amount, due: str, paid: str | None = None) -> None:
    payload = {
        "title": title,
        "titolo": title,
        "section": "casa",
        "person": "",
        "due_day": None,
        "due_date": due,
        "interval": "once",
        "amount_thb": amount,
        "notes": "",
        "note": "",
        "status": "paid" if paid else "pending",
    }
    conn = sqlite3.connect(logic.DB_PATH)
    try:
        conn.execute(
            "INSERT INTO items (id, external_id, section, payload, created_at, updated_at) VALUES (?, ?, 'casa', ?, ?, ?)",
            (external_id, external_id, json.dumps(payload), "2026-01-01T00:00:00+07:00", "2026-01-01T00:00:00+07:00"),
        )
        if paid:
            conn.execute(
                "INSERT INTO history (item_id, kind, period, paid_at, note, amount_thb, created_at) VALUES (?, 'pagato', ?, ?, '', NULL, ?)",
                (external_id, paid[:7], paid, paid + "T00:00:00+07:00"),
            )
        conn.commit()
    finally:
        conn.close()


def test_baht_format():
    assert logic.format_baht(92468) == "฿92.468"
    assert logic.format_baht(92.5) == "฿92,5"
    assert logic.format_baht(1000) == "฿1.000"
    assert logic.format_baht(None) == ""
    assert logic.amount_label({"amount_thb": None}) == ""
    assert logic.amount_label({}) == ""


def test_section_amount_and_closed_block():
    today = logic.today_bkk()
    this_month = today.isoformat()
    insert("open-money", "Con cifra", 92468, "2026-12-04")
    insert("open-empty", "Senza cifra", None, "2026-12-08")
    insert("paid-now", "Gia pagata", 1000, "2026-12-01", paid=this_month)
    html = server.page_section("casa")
    assert "4 dic · ฿92.468" in html
    assert "8 dic" in html
    assert "฿" not in html.split("Senza cifra", 1)[1].split("Gia pagata", 1)[0]
    assert "baht" not in html.lower()
    assert "<details class=\"paid-block\">" in html
    assert " open" not in html.split("<details", 1)[1].split(">", 1)[0]
    assert "Pagati questo mese" in html
    assert ">1</span>" in html
    assert html.index("Con cifra") < html.index("Senza cifra") < html.index("Pagati questo mese")
    assert html.index("Pagati questo mese") < html.index("Gia pagata")
    home = server.page_home()
    assert "4 dic · ฿92.468" in home
    assert "baht" not in home.lower()


def test_upgrade_from_104_keeps_rows():
    conn = sqlite3.connect(logic.DB_PATH)
    cols = [row[1] for row in conn.execute("PRAGMA table_info(items)")]
    conn.close()
    insert("keep-104", "Resta", None, "2027-01-02")
    logic.ensure_storage()
    conn = sqlite3.connect(logic.DB_PATH)
    try:
        assert conn.execute("SELECT 1 FROM items WHERE external_id = ?", ("keep-104",)).fetchone()
        assert [row[1] for row in conn.execute("PRAGMA table_info(items)")] == cols
        conn.execute("DELETE FROM history")
        conn.execute("DELETE FROM items")
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0
    finally:
        conn.close()
