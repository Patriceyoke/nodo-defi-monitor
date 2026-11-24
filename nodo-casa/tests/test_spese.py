"""Spending totals come from payments, and card spend is optional."""
from __future__ import annotations

import json
import sqlite3

import pytest


def test_chart_uses_payments_and_cards_do_not_double_count(casa):
    logic, server = casa
    today = logic.today_bkk()
    paid = today.isoformat()
    conn = sqlite3.connect(logic.DB_PATH)
    conn.execute(
        "INSERT INTO items (id, external_id, section, payload, created_at, updated_at) VALUES (?, ?, 'casa', ?, ?, ?)",
        (
            "bill",
            "bill",
            json.dumps({
                "title": "Luce", "titolo": "Luce", "giorno": 2, "interval": "monthly",
                "amount_thb": 99999, "category": "bollette", "status": "pending",
            }),
            "2026-01-01T00:00:00+07:00",
            "2026-01-01T00:00:00+07:00",
        ),
    )
    conn.execute(
        "INSERT INTO history (item_id, kind, period, paid_at, note, amount_thb, created_at) VALUES ('bill', 'pagato', ?, ?, '', '400', ?)",
        (paid[:7], paid, paid + "T00:00:00+07:00"),
    )
    conn.execute(
        "INSERT INTO items (id, external_id, section, payload, created_at, updated_at) VALUES ('gara', 'gara', 'garage', ?, ?, ?)",
        (
            json.dumps({"title": "Tagliando", "veicolo": "Auto", "tipo": "tagliando", "data": "2026-04-01", "interval": "yearly"}),
            "2026-01-01T00:00:00+07:00",
            "2026-01-01T00:00:00+07:00",
        ),
    )
    conn.execute(
        "INSERT INTO history (item_id, kind, period, paid_at, note, amount_thb, created_at) VALUES ('gara', 'fatto', '2026-04-01', ?, '', '100', ?)",
        (paid, paid + "T00:00:00+07:00"),
    )
    card = conn.execute("SELECT id FROM cards WHERE name = 'Bangkok Bank'").fetchone()[0]
    conn.execute(
        "INSERT INTO card_months (card_id, month, amount_thb, currency, amount_original, rate_to_thb, note, updated_at) VALUES (?, ?, '250', 'THB', '250', NULL, '', ?)",
        (card, paid[:7], paid + "T00:00:00+07:00"),
    )
    conn.commit()
    conn.close()

    plain = logic.spending("mese", False)
    with_cards = logic.spending("mese", True)
    assert plain["empty"] is False
    ids = {row["id"] for row in plain["slices"]}
    assert ids == {"bollette", "garage"}
    assert "carte" not in ids
    assert sum(float(row["amount_thb"]) for row in plain["slices"]) == 500
    assert sum(float(row["amount_thb"]) for row in with_cards["slices"]) == 500
    assert "carte" not in {row["id"] for row in with_cards["slices"]}
    assert any(
        line["name"] == "Bangkok Bank" and line["currency"] == "THB" and line["amount"] == "250"
        for line in with_cards["card_lines"]
    )
    home = server.page_home()
    nav = home.split("<nav>", 1)[1].split("</nav>", 1)[0]
    assert nav.index("Casa") < nav.index("Garage") < nav.index("Documenti") < nav.index("Visto") < nav.index("Spese")
    assert "Carte" not in nav
    page = server.page_spese("mese", True)
    assert "Casa / bollette" in page
    assert "Bangkok Bank" in page
    assert "฿250" in page
    hidden = server.page_spese("mese", False)
    assert "Casa / bollette" in hidden
    assert "Registra il mese" not in hidden
    with pytest.raises(logic.Invalid) as caught:
        logic.import_items({"items": [{
            "external_id": "no-carte",
            "section": "casa",
            "title": "No",
            "person": "",
            "due_day": 1,
            "due_date": "",
            "interval": "monthly",
            "amount_thb": None,
            "notes": "",
            "status": "pending",
            "category": "carte",
        }]})
    assert caught.value.field == "category"
