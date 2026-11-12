"""Totals, income and the positions accordion setting stay consistent."""
from __future__ import annotations

from app import markets
from app.portfolio import consistency_notes, income_figures, position_summary, stats


def _pack(rows):
    st = stats(rows)
    return {
        "total_usd": st["total_usd"],
        "btc_usd": 0.0,
        "defi_usd": st["defi_usd"] + st["wallet_usd"],
    }


def test_owner_totals_weighted_apy_and_income_match():
    positions = [
        {"owner": "io", "protocol": "Aave", "asset": "USDC", "usd": 100, "apy": 10, "apy30": 8},
        {"owner": "moglie", "protocol": "Compound", "asset": "USDC", "usd": 50, "apy": 4, "apy30": 4},
        {"owner": "io", "protocol": "Wallet", "asset": "ETH", "usd": 25, "apy": None},
    ]
    totals = {
        "io": _pack([p for p in positions if p["owner"] == "io"]),
        "moglie": _pack([p for p in positions if p["owner"] == "moglie"]),
        "tutti": _pack(positions),
    }
    income = {"tutti": income_figures(positions)}
    assert consistency_notes(positions, totals, income) == []
    assert totals["io"]["total_usd"] + totals["moglie"]["total_usd"] == totals["tutti"]["total_usd"]
    assert income["tutti"]["apy_year"] == 8
    assert income["tutti"]["year_usd"] == 12
    summary = position_summary(positions)
    assert summary["io"]["count"] == 2
    assert summary["moglie"]["usd"] == 50


def test_a_broken_total_is_reported():
    positions = [{"owner": "io", "protocol": "Aave", "asset": "USDC", "usd": 100, "apy": 10, "apy30": 10}]
    totals = {
        "io": _pack(positions),
        "moglie": {"total_usd": 0.0, "btc_usd": 0.0},
        "tutti": {"total_usd": 1.0, "btc_usd": 0.0},
    }
    income = {"tutti": income_figures(positions)}
    notes = consistency_notes(positions, totals, income)
    assert any("Combinato" in note for note in notes)


def test_market_cache_clears_without_touching_saved_rows(tmp_path, monkeypatch):
    from app import config, db

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "monitor.db")
    monkeypatch.setattr(config, "_READY", True)
    monkeypatch.setattr(config, "DATA_PERSISTENT", True)
    db.init_db()
    db.add_evm("Casa", "0x" + "ab" * 20, "io")
    db.set_setting("positions_open", "0")
    db.cache_set("nfts:abc", {"items": []})
    markets._CACHE["https://example.test"] = (1.0, {"ok": True})
    markets.invalidate()
    assert markets._CACHE == {}
    from app import nft
    nft.invalidate_caches()
    db.init_db()
    assert db.list_evm()[0]["label"] == "Casa"
    assert db.get_setting("positions_open") == "0"
    assert db.cache_get("nfts:abc") is None
