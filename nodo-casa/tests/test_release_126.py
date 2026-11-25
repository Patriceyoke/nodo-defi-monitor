"""1.2.6: hidden digits, backups, safe re-import, and the 90-day reminder."""
from __future__ import annotations

import json
import sqlite3
from datetime import timedelta


def _tx(merchant: str, amount: int, day: str) -> dict:
    return {
        "date": day,
        "merchant": merchant,
        "direction": "out",
        "statement_amount": amount,
        "section": "activity",
        "last4": "1881",
    }


def _item(external_id: str, section: str, title: str, due: str, interval: str = "yearly") -> dict:
    return {
        "external_id": external_id,
        "section": section,
        "title": title,
        "person": "Demo",
        "due_day": None,
        "due_date": due,
        "interval": interval,
        "amount_thb": None,
        "notes": "",
        "status": "pending",
    }


def _ids(logic, external_id: str, month: str) -> list[str]:
    conn = sqlite3.connect(logic.DB_PATH)
    try:
        rows = conn.execute(
            """
            SELECT t.id FROM card_tx t
            JOIN cards c ON c.id = t.card_id
            WHERE c.external_id = ? AND t.month = ?
            ORDER BY t.merchant
            """,
            (external_id, month),
        ).fetchall()
    finally:
        conn.close()
    return [row[0] for row in rows]


def test_reimport_keeps_identical_rows_and_an_empty_list(casa):
    logic, _server = casa
    month = logic.today_bkk().strftime("%Y-%m")
    day = logic.today_bkk().isoformat()
    payload = {
        "cards": [{
            "external_id": "etherfi",
            "name": "Ether.fi",
            "type": "debito",
            "currency": "USD",
            "month": month,
            "transactions": [_tx("Negozio demo", 12, day)],
        }]
    }
    logic.import_items(payload)
    first = _ids(logic, "etherfi", month)
    assert len(first) == 1
    logic.import_items(payload)
    assert _ids(logic, "etherfi", month) == first
    logic.import_items({
        "cards": [{
            "external_id": "etherfi",
            "month": month,
            "transactions": [],
        }]
    })
    assert _ids(logic, "etherfi", month) == first
    logic.import_items({
        "cards": [{
            "external_id": "etherfi",
            "month": month,
            "transactions": [_tx("Altro negozio", 4, day)],
        }]
    })
    again = _ids(logic, "etherfi", month)
    assert again != first
    conn = sqlite3.connect(logic.DB_PATH)
    try:
        names = [row[0] for row in conn.execute(
            "SELECT merchant FROM card_tx WHERE month = ?",
            (month,),
        )]
    finally:
        conn.close()
    assert names == ["Altro negozio"]
    assert (logic.DATA_DIR / "casa.db.bak.1").is_file()
    conn = sqlite3.connect(logic.DB_PATH)
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        indexed = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = 'card_tx_card_month'"
        ).fetchone()
    finally:
        conn.close()
    assert mode == "wal"
    assert indexed


def test_pages_and_export_omit_digits_and_the_home_lists_deadlines(casa):
    logic, server = casa
    today = logic.today_bkk()
    logic.import_items({
        "items": [
            _item("doc-demo", "documenti", "Passaporto demo", (today + timedelta(days=40)).isoformat()),
            _item("visto-demo", "immigrazione", "Visto demo", (today + timedelta(days=20)).isoformat()),
            _item("doc-lontano", "documenti", "Libretto lontano", (today + timedelta(days=200)).isoformat()),
        ],
        "cards": [{
            "external_id": "etherfi",
            "name": "Ether.fi",
            "type": "debito",
            "currency": "USD",
            "month": today.strftime("%Y-%m"),
            "plastics": [{"last4": "1881", "label": "virtuale"}],
            "transactions": [_tx("Negozio demo", 12, today.isoformat())],
        }],
    })
    home = server.page_home()
    assert "Entro 90 giorni" in home
    assert "Passaporto demo" in home
    assert "Visto demo" in home
    assert "Libretto lontano" not in home.split("Entro 90 giorni", 1)[1].split("Prossimi 30 giorni", 1)[0]
    ether = next(card for card in logic.list_cards() if card["external_id"] == "etherfi")
    page = server.page_card(ether)
    assert "1881" not in page
    assert "virtuale" in page
    exported = json.dumps(logic.export_all())
    assert "1881" not in exported
    assert "last4" not in exported
