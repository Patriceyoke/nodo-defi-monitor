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


def _closed_day(logic) -> str:
    year, month = logic.previous_month()
    return f"{year:04d}-{month:02d}-15"


def _tx(merchant, amount, currency, direction="out", section="activity", category=None, fx=None, day=""):
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
    month = _closed_day(logic)[:7]
    day = _closed_day(logic)
    logic.import_items({"cards": [
        {
            "external_id": "kast",
            "name": "Kast Finance",
            "type": "debito",
            "currency": "USD",
            "month": month,
            "transactions": [
                _tx("Netflix", 50, "USD", fx=33, day=day),
                _tx("Received salary", 20, "USD", direction="in", day=day),
                _tx("KAST cash reward", 2, "USD", day=day),
                _tx("Pagamento del saldo precedente", 9, "USD", day=day),
            ],
        },
        {
            "external_id": "bbl-visa",
            "name": "Bangkok Bank",
            "type": "credito",
            "currency": "THB",
            "month": month,
            "transactions": [
                _tx("Grab", 98, "THB", day=day),
                _tx("Pharmacy", 2, "THB", day=day),
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
    assert "Mese scorso" in page
    assert "Questo mese" not in page
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
    month = _closed_day(logic)[:7]
    day = _closed_day(logic)
    logic.import_items({"cards": [{
        "external_id": "etherfi",
        "name": "Ether.fi",
        "type": "debito",
        "currency": "USD",
        "month": month,
        "money_in": 1,
        "transactions": [_tx(name, 10, "USD", category=category, day=day) for name, category in named],
    }]})
    bars = logic.card_out_charts("mese")
    usd = next(row for row in bars if row["currency"] == "USD")
    assert len(usd["bars"]) == 8
    assert sum(bar["percent"] for bar in usd["bars"]) >= 99
    assert any(bar["category"] == "Altro" for bar in usd["bars"])
    ether = next(card for card in logic.list_cards() if card["external_id"] == "etherfi")
    month = next(row for row in ether["months"] if row["month"] == _closed_day(logic)[:7])
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
        "month": _closed_day(logic)[:7],
        "transactions": [_tx(name, 10, "USD", category="activity", day=_closed_day(logic)) for name, _category in named],
    }]})
    conn = sqlite3.connect(logic.DB_PATH)
    again = conn.execute("SELECT category FROM card_tx WHERE merchant = 'Grab'").fetchone()[0]
    conn.close()
    assert again == "Salute"
    assert "Apertura" in server.page_spese("mese", True)


def test_january_opens_december_and_the_merchant_list_follows(casa, monkeypatch):
    from datetime import date

    logic, server = casa
    _clear(logic)
    monkeypatch.setattr(logic, "today_bkk", lambda: date(2026, 1, 15))
    assert logic.previous_month(date(2026, 1, 3)) == (2025, 12)
    logic.import_items({"cards": [{
        "external_id": "kast",
        "name": "Kast Finance",
        "type": "debito",
        "currency": "USD",
        "month": "2025-12",
        "transactions": [_tx("Grab dicembre", 12, "USD", day="2025-12-10")],
    }, {
        "external_id": "kast",
        "name": "Kast Finance",
        "type": "debito",
        "currency": "USD",
        "month": "2026-01",
        "transactions": [_tx("Netflix gennaio", 8, "USD", day="2026-01-04")],
    }]})
    opened = {bar["category"] for bar in logic.card_out_charts("mese")[0]["bars"]}
    year = {bar["category"] for bar in logic.card_out_charts("anno")[0]["bars"]}
    assert opened == {"Trasporti"}
    assert year == {"Abbonamenti"}
    assert logic.card_out_merchants("mese", "USD", "Trasporti") == [{"name": "Grab dicembre", "count": 1}]
    assert logic.card_out_merchants("anno", "USD", "Abbonamenti") == [{"name": "Netflix gennaio", "count": 1}]
    assert logic.card_out_merchants("mese", "USD", "Abbonamenti") == []
    page = server.page_spese()
    assert "Mese scorso" in page
    assert "Questo mese" not in page
    assert "periodo=mese" in page
    detail = server.page_card_merchants("mese", "USD", "Trasporti")
    assert "Grab dicembre" in detail
    assert "Netflix gennaio" not in detail
