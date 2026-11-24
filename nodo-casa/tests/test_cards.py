"""Cards store monthly spend, never a card number, and are seeded only once."""
from __future__ import annotations

import json
import sqlite3

import pytest


def test_seed_has_no_numbers_and_delete_is_not_reseeded(casa):
    logic, server = casa
    cards = logic.list_cards()
    assert [card["name"] for card in cards] == ["Bangkok Bank", "Ether.fi", "Kast Finance"]
    conn = sqlite3.connect(logic.DB_PATH)
    columns = [row[1] for row in conn.execute("PRAGMA table_info(cards)")]
    conn.close()
    for banned in ("number", "pan", "last4", "cvv", "iban"):
        assert banned not in columns
    victim = next(card for card in cards if card["name"] == "Ether.fi")
    assert logic.delete_card(victim["id"]) is True
    logic.ensure_storage()
    names = [card["name"] for card in logic.list_cards()]
    assert "Ether.fi" not in names
    assert names.count("Bangkok Bank") == 1
    page = server.page_cards()
    assert "Registra il mese" in page
    assert "Ether.fi" not in page


def test_import_cards_items_only_usd_rate_and_rejected_numbers(casa):
    logic, server = casa
    items_only = {"items": [logic.IMPORT_EXAMPLE["items"][0]]}
    result = logic.import_items(items_only)
    assert result["created"] and result["cards_created"] == [] and result["card_months"] == 0
    cards_only = {"cards": logic.IMPORT_EXAMPLE["cards"]}
    again = logic.import_items(cards_only)
    assert again["created"] == []
    assert again["card_months"] == 2
    bangkok = next(card for card in logic.list_cards() if card["name"] == "Bangkok Bank")
    assert bangkok["months"][0]["amount_thb"] == "15400"
    kast = next(card for card in logic.list_cards() if card["name"] == "Kast Finance")
    assert kast["months"][0]["amount_thb"] == "2794.56"
    with pytest.raises(logic.Invalid) as missing:
        logic.import_items({"cards": [{
            "name": "Sola",
            "type": "debito",
            "currency": "EUR",
            "month": "2026-11",
            "amount": 10,
        }]})
    assert missing.value.field == "rate_to_thb"
    with pytest.raises(logic.Invalid) as secret:
        logic.import_items({"cards": [{
            "name": "Bangkok Bank",
            "type": "credito",
            "month": "2026-12",
            "amount": 1,
            "currency": "THB",
            "pan": "4111111111111111",
        }]})
    assert secret.value.field == "pan"
    with pytest.raises(logic.Invalid) as named:
        logic.create_card({"name": "4111111111111111", "kind": "debito", "currency": "USD"})
    assert named.value.field == "name"
    exported = logic.export_all()
    assert "cards" in exported
    assert "4111111111111111" not in json.dumps(exported["cards"])
    schema_page = server.page_schema()
    assert "pwa-acqua" in schema_page
    assert "Kast Finance" in schema_page
    assert "every_unit" in schema_page
