"""Cards are accounts. A statement is not one amount and one rate."""
from __future__ import annotations

import json
import sqlite3

import pytest


def test_seed_ids_survive_and_are_not_duplicated(casa):
    logic, server = casa
    cards = logic.list_cards()
    assert {card["external_id"] for card in cards} == {"kast", "etherfi", "bbl-visa"}
    assert [card["name"] for card in cards] == ["Bangkok Bank", "Ether.fi", "Kast Finance"]
    conn = sqlite3.connect(logic.DB_PATH)
    conn.execute("UPDATE cards SET external_id = NULL")
    conn.commit()
    conn.close()
    logic.ensure_storage()
    again = logic.list_cards()
    assert len(again) == 3
    assert {card["external_id"] for card in again} == {"kast", "etherfi", "bbl-visa"}
    ether = next(card for card in again if card["external_id"] == "etherfi")
    assert logic.delete_card(ether["id"]) is True
    logic.ensure_storage()
    names = [card["name"] for card in logic.list_cards()]
    assert names.count("Ether.fi") == 0
    assert names.count("Bangkok Bank") == 1
    page = server.page_cards()
    assert "Registra il mese" in page
    assert "Ether.fi" not in page


def test_import_upserts_by_external_id_and_keeps_statement_shape(casa):
    logic, server = casa
    items_only = {"items": [logic.IMPORT_EXAMPLE["items"][0]]}
    result = logic.import_items(items_only)
    assert result["created"] and result["cards_created"] == [] and result["card_months"] == 0
    body = {"cards": logic.IMPORT_EXAMPLE["cards"]}
    first = logic.import_items(body)
    second = logic.import_items(body)
    assert first["created"] == [] and second["created"] == []
    assert second["cards_created"] == []
    assert len([card for card in logic.list_cards() if card["name"] == "Kast Finance"]) == 1
    bangkok = next(card for card in logic.list_cards() if card["external_id"] == "bbl-visa")
    assert bangkok["months"] == []
    ether = next(card for card in logic.list_cards() if card["external_id"] == "etherfi")
    assert ether["opening"] if False else ether["months"][0]["opening"] is None
    assert ether["months"][0]["closing"] is None
    assert ether["months"][0]["money_in"] == "5"
    assert ether["months"][0]["money_out"] == "40"
    assert [item["last4"] for item in ether["plastics"]] == ["1881", "1882"]
    kast = next(card for card in logic.list_cards() if card["external_id"] == "kast")
    assert kast["months"][0]["opening"] == "1000"
    assert kast["months"][0]["closing"] == "1050"
    assert kast["months"][0]["spend"] == "12.5"
    assert "4111111111111111" not in json.dumps(logic.export_all()["cards"])
    schema = server.page_schema()
    assert "external_id" in schema and "money_out" in schema and "bbl-visa" in schema
    with pytest.raises(logic.Invalid) as secret:
        logic.import_items({"cards": [{
            "external_id": "bbl-visa",
            "name": "Bangkok Bank",
            "type": "credito",
            "pan": "4111111111111111",
        }]})
    assert secret.value.field == "pan"
    with pytest.raises(logic.Invalid) as account:
        logic.import_items({"cards": [{
            "external_id": "bbl-visa",
            "account_id": "99887766",
        }]})
    assert account.value.field == "account_id"


def test_pending_duplicates_rewards_and_missing_fx(casa):
    logic, server = casa
    logic.import_items({"cards": [{
        "external_id": "etherfi",
        "name": "Ether.fi",
        "type": "debito",
        "month": logic.today_bkk().strftime("%Y-%m"),
        "currency": "USD",
        "money_in": 1,
        "money_out": 99,
        "transactions": [
            {"date": "2026-10-01", "merchant": "Shop", "direction": "out", "statement_amount": 10, "section": "activity", "last4": "1881"},
            {"date": "2026-10-01", "merchant": "Shop", "direction": "out", "statement_amount": 10, "section": "pending", "last4": "1881"},
            {"date": "2026-10-02", "merchant": "Ether.fi cashback", "direction": "out", "statement_amount": 3, "section": "activity", "last4": "1881"},
        ],
    }, {
        "external_id": "kast",
        "name": "Kast Finance",
        "type": "debito",
        "month": logic.today_bkk().strftime("%Y-%m"),
        "currency": "USD",
        "transactions": [
            {"date": "2026-10-03", "merchant": "Received salary", "direction": "in", "statement_amount": 20, "last4": "4242"},
            {"date": "2026-10-04", "merchant": "KAST cash reward", "direction": "out", "statement_amount": 2, "last4": "4242"},
            {"date": "2026-10-05", "merchant": "Cafe", "direction": "out", "statement_amount": 8, "fx_rate": 32, "last4": "4242"},
        ],
    }]})
    month = logic.today_bkk().strftime("%Y-%m")
    ether = next(card for card in logic.list_cards() if card["external_id"] == "etherfi")
    current = next(row for row in ether["months"] if row["month"] == month)
    merchants = [tx["merchant"] for tx in current["transactions"]]
    assert merchants.count("Shop") == 1
    assert current["spend"] == "10"
    assert current["amount_thb"] is None
    kast = next(card for card in logic.list_cards() if card["external_id"] == "kast")
    kast_month = next(row for row in kast["months"] if row["month"] == month)
    assert kast_month["spend"] == "8"
    home = server.page_home("mese", True)
    assert "18 USD" in home
    assert "฿0" not in home
    page = server.page_cards()
    assert "฿0" not in page
    assert "•••• 1881" in server.page_card(ether)
    assert "4111111111111111" not in server.page_card(ether)
