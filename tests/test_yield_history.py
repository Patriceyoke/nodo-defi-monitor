"""Hourly snapshots and estimated interest. Addresses here are the usual fakes."""
from datetime import datetime

from app import db, portfolio, yield_history
from app.switches import compare_rates
from app.yield_history import BANGKOK, YEAR_SECONDS, accrual_amount

OWNER = "yield-test"


def _snap(ts, amount, value, apy=5.0, label="Esempio", protocol="Aave", asset="USDC"):
    return {
        "ts": int(ts),
        "owner": OWNER,
        "total_usd": value,
        "wallets": [{"label": label, "owner": "io", "total_usd": value}],
        "positions": [{
            "label": label,
            "owner": "io",
            "protocol": protocol,
            "asset": asset,
            "amount": amount,
            "value_usd": value,
            "apy": apy,
            "chain": "ethereum",
        }],
    }


def _stamp(year, month, day, hour=12):
    return int(datetime(year, month, day, hour, tzinfo=BANGKOK).timestamp())


def test_save_history_is_hourly_and_prunes(tmp_path, monkeypatch):
    from app import config

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "monitor.db")
    monkeypatch.setattr(config, "_READY", True)
    db.init_db()
    start = 1_700_000_000
    assert db.save_history(OWNER, {"total_usd": 1}, ts=start) is True
    assert db.save_history(OWNER, {"total_usd": 2}, ts=start + 3599) is False
    assert db.save_history(OWNER, {"total_usd": 3}, ts=start + 3600) is True
    assert db.save_history(OWNER, {"total_usd": 4}, ts=start + 3601, force=True) is True
    old = start - 401 * 86400
    assert db.save_history(OWNER, {"total_usd": 0}, ts=old, force=True) is True
    assert db.save_history(OWNER, {"total_usd": 9}, ts=start + 7201) is True
    kept = db.get_history(OWNER, days=4000)
    assert [row["total_usd"] for row in kept] == [1, 3, 4, 9]


def test_price_move_is_not_interest_and_a_deposit_is_excluded():
    start = _stamp(2026, 11, 1)
    later = start + 20 * 86400
    expected = 10_000 * ((1.05) ** ((later - start) / YEAR_SECONDS) - 1)
    rows = [
        _snap(start, 10_000, 10_000),
        _snap(later, 10_000, 12_000),
    ]
    built = yield_history.build(rows, now=later)
    assert built["realized_yield"]["estimate"] is True
    assert built["realized_yield"]["current_month_usd"] == 0
    assert built["wallet_series"]["points"][-1]["total_usd"] == 12_000

    accrued = _snap(later, 10_000 + expected, 10_000 + expected)
    gained = yield_history.build([rows[0], accrued], now=later)
    assert gained["realized_yield"]["current_month_usd"] > 0
    assert abs(gained["realized_yield"]["current_month_usd"] - expected) < 0.05
    assert gained["realized_yield"]["by_wallet"][0]["label"] == "Esempio"
    assert gained["realized_yield"]["months"][0]["month"] == "2026-11"

    deposited = _snap(later, 15_000, 15_000)
    assert yield_history.build([rows[0], deposited], now=later)["realized_yield"]["current_month_usd"] == 0
    withdrawn = _snap(later, 9_000, 9_000)
    assert yield_history.build([rows[0], withdrawn], now=later)["realized_yield"]["current_month_usd"] == 0
    assert accrual_amount(10_000, 9_000, 5, later - start) == 0


def test_interest_lands_in_the_bangkok_month_of_the_later_snapshot():
    before = _stamp(2026, 10, 31, 22)
    after = _stamp(2026, 11, 1, 8)
    expected = 10_000 * ((1.05) ** ((after - before) / YEAR_SECONDS) - 1)
    built = yield_history.build([
        _snap(before, 10_000, 10_000),
        _snap(after, 10_000 + expected, 10_000 + expected),
    ], now=after)
    assert built["realized_yield"]["months"] == [
        {"month": "2026-11", "usd": built["realized_yield"]["current_month_usd"]},
    ]
    october = yield_history.build([
        _snap(before, 10_000, 10_000),
        _snap(after, 10_000 + expected, 10_000 + expected),
    ], now=before)
    assert october["realized_yield"]["current_month_usd"] == 0
    assert october["realized_yield"]["months"][0]["month"] == "2026-11"


def test_history_snapshot_keeps_amounts_and_wallet_totals():
    payload = portfolio.history_snapshot([
        {"owner": "io", "label": "Esempio", "protocol": "Aave V4", "asset": "USDC", "amount": 15038, "usd": 15038, "apy": 5.19, "chain": "ethereum"},
        {"owner": "io", "label": "Esempio", "protocol": "Wallet", "asset": "ETH", "amount": 0.1, "usd": 200, "apy": None, "chain": "ethereum"},
    ])
    assert payload["total_usd"] == 15238
    assert payload["wallets"] == [{"label": "Esempio", "owner": "io", "total_usd": 15238}]
    assert payload["positions"][0]["amount"] == 15038
    assert payload["positions"][0]["value_usd"] == 15038
    assert "address" not in payload["positions"][0]


def test_failed_view_has_empty_history_keys():
    view = portfolio.failed_view("tutti", ["rete"])
    assert view["wallet_series"] == {"wallets": [], "points": []}
    assert view["realized_yield"]["estimate"] is True
    assert view["realized_yield"]["months"] == []
    assert view["realized_yield"]["by_wallet"] == []
    assert view["comparisons"] == []


def test_comparison_lists_only_equal_or_lower_risk_after_gas():
    src = {
        "owner": "io", "label": "Esempio", "protocol": "Aave", "asset": "USDC",
        "usd": 20_000, "apy": 3.0, "apy30": 3.0, "alternatives": [
            {"protocol": "Compound", "asset": "USDC", "apy": 5.0, "apy30": 5.0, "owner": None, "tvl_usd": 200_000_000},
            {"protocol": "Sky", "asset": "sUSDS", "apy": 8.0, "apy30": 8.0, "owner": None, "tvl_usd": 200_000_000},
            {"protocol": "Spark", "asset": "USDC", "apy": 7.0, "apy30": 7.0, "owner": None, "tvl_usd": 200_000_000},
            {"protocol": "Sky", "asset": "stUSDS", "apy": 44.0, "apy30": 40.0, "owner": None, "tvl_usd": 80_000_000},
            {"protocol": "Fluid", "asset": "USDC", "apy": 9.0, "apy30": 9.0, "owner": None, "tvl_usd": 200_000_000},
        ],
    }
    cards = compare_rates([src], gas_usd=1)
    assert len(cards) == 1
    assert cards[0]["wallet_label"] == "Esempio"
    assert cards[0]["protocol"] == "Aave"
    assert [item["protocol"] for item in cards[0]["alternatives"]] == ["Compound"]
    assert cards[0]["alternatives"][0]["net_year_usd"] == round(20_000 * (5.0 - 3.0) / 100 - 1, 2)
    assert cards[0]["empty_text"] == ""
    blocked = compare_rates([src], gas_usd=500)
    assert blocked[0]["alternatives"] == []
    assert blocked[0]["empty_text"] == "Nessuna alternativa migliore a pari rischio"
