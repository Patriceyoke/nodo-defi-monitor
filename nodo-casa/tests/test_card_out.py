"""Card outflow chart: merchant categories, one chart per currency."""
from __future__ import annotations

import sqlite3


def test_merchant_rules_use_word_boundaries(casa):
    logic, _server = casa
    assert logic.merchant_category("Grab airport") == "Trasporti"
    assert logic.merchant_category("The Barber Shop") == "Altro"
    assert logic.merchant_category("construction supplies") == "Altro"
    assert logic.merchant_category("Cafe") == "Ristoranti"
    assert logic.merchant_category("cafeteria") == "Altro"
    assert logic.merchant_category("True") == "Casa"
    assert logic.merchant_category("TrueMoney wallet") == "Altro"
    assert logic.merchant_category("7-11 night") == "Spesa"
    assert logic.merchant_category("AMZ_market") == "Acquisti"
    assert logic.merchant_category("Netflix") == "Abbonamenti"
    assert logic.merchant_category("Pharmacy") == "Salute"


def _clear(logic) -> None:
    conn = sqlite3.connect(logic.DB_PATH)
    conn.execute("DELETE FROM card_tx")
    conn.commit()
    conn.close()


def _tx(merchant, amount, currency, direction="out", section="activity", category=None, fx=None, day="2026-11-02"):
    row = {
        "date": day,
        "merchant": merchant,
        "direction": direction,
        "statement_amount": amount,
        "original_currency": currency,
        "section": section,
    }
    if category is not None:
        row["category"] = category
    if fx is not None:
        row["fx_rate"] = fx
    return row


def test_charts_stay_in_one_currency_and_fold_small_shares(casa):
    logic, server = casa
    _clear(logic)
    logic.import_items({"cards": [
        {
            "external_id": "kast",
            "name": "Kast Finance",
            "type": "debito",
            "currency": "USD",
            "month": "2026-11",
            "transactions": [
                _tx("Netflix", 50, "USD", fx=33),
                _tx("Received salary", 20, "USD", direction="in"),
                _tx("KAST cash reward", 2, "USD"),
                _tx("Pagamento del saldo precedente", 9, "USD"),
            ],
        },
        {
            "external_id": "bbl-visa",
            "name": "Bangkok Bank",
            "type": "credito",
            "currency": "THB",
            "month": "2026-11",
            "transactions": [
                _tx("Grab", 98, "THB"),
                _tx("Pharmacy", 2, "THB"),
            ],
        },
    ]})
    charts = {row["currency"]: row["bars"] for row in logic.card_out_charts("mese")}
    assert set(charts) == {"USD", "THB"}
    assert charts["USD"] == [{"category": "Abbonamenti", "percent": 100}]
    thb = {bar["category"]: bar["percent"] for bar in charts["THB"]}
    assert thb["Trasporti"] == 98
    assert thb["Altro"] == 2
    assert "Salute" not in thb
    page = server.page_spese("mese", True)
    assert "Uscite USD" in page
    assert "Uscite THB" in page
    assert "Casa / bollette" not in page or "Uscite" in page
    hidden = server.page_spese("mese", False)
    assert "Uscite USD" not in hidden
    assert "Apertura" in page and "Chiusura" in page
    merchants = logic.card_out_merchants("mese", "USD", "Abbonamenti")
    assert merchants == [{"name": "Netflix", "count": 1}]
    detail = server.page_card_merchants("mese", "USD", "Abbonamenti")
    assert "Netflix · 1" in detail
    assert "••••" not in detail
    assert "4242" not in detail


def test_eight_bars_and_a_saved_category_survives_refresh(casa):
    logic, server = casa
    _clear(logic)
    named = [
        ("Grab", None),
        ("Lotus", None),
        ("KFC", None),
        ("Netflix", None),
        ("AIS", None),
        ("Pharmacy", None),
        ("Lazada", None),
        ("Museo", "Viaggi"),
        ("Corso", "Scuola"),
    ]
    logic.import_items({"cards": [{
        "external_id": "etherfi",
        "name": "Ether.fi",
        "type": "debito",
        "currency": "USD",
        "month": "2026-11",
        "money_in": 1,
        "transactions": [_tx(name, 10, "USD", category=category) for name, category in named],
    }]})
    bars = logic.card_out_charts("mese")
    usd = next(row for row in bars if row["currency"] == "USD")
    assert len(usd["bars"]) == 8
    assert sum(bar["percent"] for bar in usd["bars"]) >= 99
    assert any(bar["category"] == "Altro" for bar in usd["bars"])
    ether = next(card for card in logic.list_cards() if card["external_id"] == "etherfi")
    month = next(row for row in ether["months"] if row["month"] == "2026-11")
    assert month["opening"] is None
    assert month["closing"] is None
    conn = sqlite3.connect(logic.DB_PATH)
    conn.execute(
        "UPDATE card_tx SET category = 'Salute' WHERE merchant = 'Grab'"
    )
    conn.execute("UPDATE card_tx SET category = NULL WHERE merchant = 'Lotus'")
    conn.commit()
    conn.close()
    logic.ensure_storage()
    conn = sqlite3.connect(logic.DB_PATH)
    grab = conn.execute("SELECT category FROM card_tx WHERE merchant = 'Grab'").fetchone()[0]
    lotus = conn.execute("SELECT category FROM card_tx WHERE merchant = 'Lotus'").fetchone()[0]
    conn.close()
    assert grab == "Salute"
    assert lotus == "Spesa"
    logic.import_items({"cards": [{
        "external_id": "etherfi",
        "name": "Ether.fi",
        "type": "debito",
        "currency": "USD",
        "month": "2026-11",
        "transactions": [_tx(name, 10, "USD", category="activity") for name, _category in named],
    }]})
    conn = sqlite3.connect(logic.DB_PATH)
    again = conn.execute("SELECT category FROM card_tx WHERE merchant = 'Grab'").fetchone()[0]
    conn.close()
    assert again == "Salute"
    assert "Apertura" in server.page_spese("mese", True)
