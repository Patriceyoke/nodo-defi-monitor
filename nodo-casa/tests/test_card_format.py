"""Card amounts stay short, and a category keeps its color and its own movements."""
from __future__ import annotations

from decimal import Decimal


def test_card_amounts_use_two_decimals_and_a_currency_mark(casa):
    logic, _server = casa
    raw = Decimal("17.60235294117647058823529412")
    assert logic.format_card_amount(raw, "USD") == "17,60 USD"
    assert logic.format_card_amount("1234.5", "THB") == "฿1.234,50"
    assert logic.format_card_amount("8", "USD") == "8,00 USD"
    assert "176023" not in logic.format_card_amount(raw, "USD")
    assert logic.display_merchant("WWW.GRAB.COM") == "Grab"
    assert logic.display_merchant("https://www.grab.com/ride") == "Grab"


def test_category_colors_do_not_change(casa):
    logic, _server = casa
    colors = dict(logic.CATEGORY_COLORS)
    assert set(colors) == set(logic.CARD_OUT_LABELS)
    assert len(set(colors.values())) == len(colors)
    assert colors["Trasporti"] == "#1d4e89"
    assert colors["Spesa"] == "#1e6b45"
    assert colors["Altro"] == "#5c5346"


def test_category_drilldown_filters_currency_and_period(casa):
    logic, server = casa
    year, month = logic.previous_month()
    day = f"{year:04d}-{month:02d}-08"
    later = f"{year:04d}-{month:02d}-20"
    logic.import_items({"cards": [
        {
            "external_id": "kast",
            "name": "Kast Finance",
            "type": "debito",
            "currency": "USD",
            "month": day[:7],
            "transactions": [
                {"date": day, "merchant": "WWW.GRAB.COM", "direction": "out", "statement_amount": 12.5, "last4": "4242"},
                {"date": later, "merchant": "Grab taxi", "direction": "out", "statement_amount": 4, "last4": "4242"},
                {"date": later, "merchant": "Netflix", "direction": "out", "statement_amount": 9, "last4": "1881"},
            ],
        },
        {
            "external_id": "bbl-visa",
            "name": "Bangkok Bank",
            "type": "credito",
            "currency": "THB",
            "month": day[:7],
            "transactions": [
                {"date": day, "merchant": "Grab", "direction": "out", "statement_amount": 80},
            ],
        },
    ]})
    rows = logic.card_category_transactions("mese", "USD", "Trasporti")
    assert [row["date"] for row in rows] == [later, day]
    assert rows[0]["merchant"] == "Grab taxi"
    assert rows[1]["merchant"] == "Grab"
    assert rows[1]["amount"] == "12,50 USD"
    assert rows[0]["card"] == "Kast Finance"
    assert all("last4" not in row for row in rows)
    assert logic.card_category_transactions("mese", "USD", "Abbonamenti")[0]["merchant"] == "Netflix"
    assert logic.card_category_transactions("mese", "THB", "Trasporti")[0]["amount"] == "฿80,00"
    assert logic.card_category_transactions("anno", "USD", "Trasporti")
    page = server.page_card_merchants("mese", "USD", "Trasporti")
    assert "4242" not in page
    assert "••••" not in page
    assert "Grab" in page
    assert "Netflix" not in page
    assert logic.CATEGORY_COLORS["Trasporti"] in page
    home = server.page_spese("mese", True)
    assert "media " not in home
    assert "movimenti" in home
    assert logic.CATEGORY_COLORS["Trasporti"] in home
    assert logic.CATEGORY_COLORS["Abbonamenti"] in home
