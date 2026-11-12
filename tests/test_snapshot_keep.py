"""A failed refresh must not replace the dashboard with an empty snapshot."""
from __future__ import annotations

import asyncio

import pytest

from app import config, db, portfolio


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "monitor.db")
    monkeypatch.setattr(config, "_READY", True)
    monkeypatch.setattr(config, "DATA_PERSISTENT", True)
    portfolio.invalidate()
    db.init_db()
    yield
    portfolio.invalidate()


def test_failed_refresh_keeps_the_last_positions(isolated_db, monkeypatch):
    portfolio._SNAPSHOT = {
        "ts": 1_700_000_000,
        "rpc_ok": True,
        "errors": [],
        "positions": [
            {"owner": "io", "protocol": "Aave", "asset": "USDC", "usd": 1000, "apy": 4, "apy30": 4},
        ],
        "switches": [],
        "security": [],
        "btc_wallets": [],
        "btc_security": [],
        "btc_price_usd": 50_000,
        "fx_thb": 35,
        "gas_usd": 1,
        "eth_price_usd": 2000,
        "node": {"configured": False, "connected": False, "detail": ""},
        "alternatives": [],
        "evm_owners": ["io"],
        "wallet_count": 1,
        "ens_names": [],
        "ens_alerts": [],
        "saved": True,
    }

    async def boom(*_args, **_kwargs):
        raise RuntimeError("btc scan timeout")

    monkeypatch.setattr(portfolio, "refresh", boom)
    view = asyncio.run(portfolio.snapshot("tutti", force=True))

    assert view["kept_previous"] is True
    assert view["positions"][0]["asset"] == "USDC"
    assert view["totals"]["io"]["defi_usd"] == 1000
    assert view["income"]["io"]["year_usd"] > 0
    assert any("btc scan timeout" in item for item in view["errors"])
    assert view["positions"]
