"""Income at the current APY: dollar slices and the compound monthly rate."""
from __future__ import annotations

import pytest

from app.portfolio import income_by_protocol, income_figures, monthly_yield_pct, stats


def test_monthly_yield_is_the_compound_twelfth():
    assert monthly_yield_pct(0) == 0
    assert monthly_yield_pct(12) == pytest.approx(((1.12 ** (1 / 12)) - 1) * 100)


def test_income_splits_the_year_and_groups_protocols():
    positions = [
        {"protocol": "Aave", "owner": "io", "usd": 10_000, "apy": 12, "apy30": 8},
        {"protocol": "Compound", "owner": "moglie", "usd": 5_000, "apy": 6, "apy30": 2},
        {"protocol": "Wallet", "owner": "io", "usd": 500, "apy": None, "apy30": None},
    ]
    figures = income_figures(positions)
    assert figures["year_usd"] == pytest.approx(10_000 * 0.12 + 5_000 * 0.06)
    assert figures["month_usd"] == pytest.approx(figures["year_usd"] / 12)
    assert figures["week_usd"] == pytest.approx(figures["year_usd"] / 52)
    assert figures["apy_month"] == pytest.approx(monthly_yield_pct(figures["apy_year"]))

    blended = stats(positions)
    # Weighted by dollars: 10_000 at 12% and 5_000 at 6% is 10%, not the plain average of 9%.
    assert blended["wapy"] == pytest.approx((10_000 * 12 + 5_000 * 6) / 15_000)
    assert blended["wapy30"] == pytest.approx((10_000 * 8 + 5_000 * 2) / 15_000)

    rows = income_by_protocol([p for p in positions if p["owner"] == "io"])
    assert [row["protocol"] for row in rows] == ["Aave"]
    assert rows[0]["year_usd"] == pytest.approx(1200)
    assert rows[0]["apy_year"] == pytest.approx(12)
