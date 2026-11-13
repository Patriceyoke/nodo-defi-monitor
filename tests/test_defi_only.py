"""Bitcoin rows may remain in the database, but the dashboard does not use them."""
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


def test_saved_bitcoin_is_ignored_and_evm_stays(isolated_db):
    db.add_evm("Casa", "0x000000000000000000000000000000000000dEaD", "io")
    db.set_setting("eth_rpc_url", "http://rpc.example")
    db.set_setting("electrs_host", "10.21.21.10")
    db.add_btc("Vecchio", "multisig", "wsh(sortedmulti(2,xpub,xpub,xpub))", "moglie")
    portfolio._SNAPSHOT = {
        "ts": 1,
        "rpc_ok": True,
        "errors": [],
        "positions": [{"owner": "io", "protocol": "Aave", "asset": "USDC", "usd": 1000, "apy": 4, "apy30": 4}],
        "switches": [],
        "security": [],
        "btc_wallets": [{"owner": "moglie", "label": "Vecchio", "btc": 2, "descriptor": "segreto"}],
        "btc_price_usd": 50,
        "fx_thb": 1,
        "gas_usd": 0,
        "eth_price_usd": 1,
        "alternatives": [],
        "evm_owners": ["io"],
        "wallet_count": 1,
        "ens_names": [],
        "ens_alerts": [],
        "saved": True,
        "sources": {},
    }
    view = asyncio.run(portfolio.snapshot("tutti"))
    assert view["totals"]["tutti"]["total_usd"] == pytest.approx(1000)
    assert "btc" not in view["totals"]["tutti"]
    assert "btc_wallets" not in view
    assert "Vecchio" not in str(view)
    assert "segreto" not in str(view)
    wallets = db.list_evm()
    assert wallets[0]["address"] == "0x000000000000000000000000000000000000dEaD"
    assert db.get_setting("eth_rpc_url") == "http://rpc.example"
    assert db.get_setting("electrs_host") == "10.21.21.10"
    assert len(db.list_btc()) == 1
    backup = db.export_backup()
    assert backup["btc"] == []
    assert backup["evm"][0]["address"] == "0x000000000000000000000000000000000000dEaD"
    assert "electrs_host" not in backup["settings"]
    assert "wsh" not in str(backup)
    imported = db.import_backup({
        "evm": [],
        "btc": [{"label": "Altro", "descriptor": "wpkh(xpub)", "owner": "io"}],
        "settings": {"electrs_host": "cambiato", "eth_rpc_url": "http://rpc.example"},
    })
    assert imported["btc_added"] == 0
    assert imported["evm_added"] == 0
    assert len(db.list_btc()) == 1
    assert db.list_btc()[0]["label"] == "Vecchio"
    assert db.get_setting("electrs_host") == "10.21.21.10"
    assert len(db.list_evm()) == 1


def test_upgrade_keeps_addresses_owners_and_settings(isolated_db):
    """Opening the database again must not drop wallets, owners, hides or settings."""
    address = "0x" + "ab" * 20
    db.add_evm("Esempio", address, "moglie")
    db.set_setting("eth_rpc_url", "http://rpc.example")
    db.set_setting("positions_open", "0")
    db.hide_approval(address, "ethereum", "0x" + "cd" * 20, "0x" + "ef" * 20, "unlimited")
    db.init_db()
    saved = db.list_evm()
    assert len(saved) == 1
    assert saved[0]["address"] == address
    assert saved[0]["owner"] == "moglie"
    assert saved[0]["label"] == "Esempio"
    assert db.get_setting("eth_rpc_url") == "http://rpc.example"
    assert db.get_setting("positions_open") == "0"
    hides = db.list_approval_hides()
    assert len(hides) == 1
    assert hides[0]["wallet"] == address.lower()
