"""A failed Bitcoin scan keeps the last balance, and the card keeps the scan detail."""
from __future__ import annotations

import asyncio

import pytest
from embit.descriptor.checksum import add_checksum

from app import bitcoin, config, db, portfolio

XPUB = "xpub661MyMwAqRbcFhCvdhTAfpEEDV58oqDvv65YNHC686NNs4KbH8YZQJWVmrfbve7aAVHzxw8bKFxA7MLeDK6BbLfkE3bqkvHLPgaGHHtYGeY"


def _descriptor() -> str:
    return add_checksum(f"wsh(sortedmulti(2,{XPUB}/0/*,{XPUB}/0/*,{XPUB}/0/*))")


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "monitor.db")
    monkeypatch.setattr(config, "_READY", True)
    monkeypatch.setattr(config, "DATA_PERSISTENT", True)
    portfolio.invalidate()
    bitcoin._REMEMBERED = None
    db.init_db()
    yield
    portfolio.invalidate()
    bitcoin._REMEMBERED = None


def _wallet() -> dict:
    return {"id": 1, "label": "Bitkey", "owner": "io", "kind": "multisig", "descriptor": _descriptor(), "role": "watch"}


def test_failed_scan_does_not_replace_the_last_balance(isolated_db, monkeypatch):
    bitcoin.save_scan_cache(
        1,
        {
            "result": {
                "id": 1,
                "btc": 0.14,
                "sats": 14_000_000,
                "complete": True,
                "balance_known": True,
                "scanned_at": 1_700_000_000,
                "owner": "io",
                "label": "Bitkey",
                "kind": "multisig",
                "status": "Sincronizzato",
                "connected": True,
                "scan": {"receive": {"scanned": 12, "last_used": 4}, "change": {"scanned": 0, "last_used": None}},
            },
            "scripts": {"keep": {"status": "abc"}},
            "branches": {"receive": {"last_used": 4}},
            "ts": 1_700_000_000,
        },
    )

    async def broken(*_args, **_kwargs):
        raise TimeoutError("tempo scaduto")

    async def online():
        return {"connected": True, "configured": True, "indexing": False, "host": "10.21.21.10", "port": 50001, "detail": "ok", "steps": []}

    monkeypatch.setattr(bitcoin, "_scan_branch", broken)
    monkeypatch.setattr(bitcoin, "electrs_status", online)
    result = asyncio.run(bitcoin.scan_wallet(_wallet(), gap=1))
    assert result["complete"] is False
    assert result["balance_known"] is False
    assert result["scan"]["receive"]["first_address"].startswith("bc1q")
    assert result["scan"]["change"]["first_address"].startswith("bc1q")
    assert result["scan"]["receive"]["first_address"] != result["scan"]["change"]["first_address"]
    assert "xpub" not in result["scan"]["error"]
    stored = bitcoin.load_scan_cache(1)
    assert stored["result"]["btc"] == 0.14
    assert stored["scripts"]["keep"]["status"] == "abc"

    seen: dict = {}

    async def unfinished(wallet, gap=None):
        return result

    monkeypatch.setattr(bitcoin, "scan_wallet", unfinished)

    def capture(wallets, _node):
        seen["wallets"] = wallets

    bitcoin.set_scan_callback(capture)
    try:
        asyncio.run(bitcoin._scan_all([_wallet()]))
    finally:
        bitcoin.set_scan_callback(None)
    assert seen["wallets"][0]["btc"] == 0.14
    assert seen["wallets"][0]["stale"] is True
    assert seen["wallets"][0]["balance_known"] is True
    assert seen["wallets"][0]["scan"]["error"]
    assert seen["wallets"][0]["scan"]["receive"]["first_address"].startswith("bc1q")


def test_finished_scan_is_merged_over_the_preview(isolated_db):
    portfolio._SNAPSHOT = {
        "ts": 1,
        "rpc_ok": True,
        "errors": [],
        "positions": [],
        "switches": [],
        "security": [],
        "btc_wallets": [
            {
                "id": 1,
                "owner": "io",
                "label": "Bitkey",
                "kind": "multisig",
                "btc": None,
                "balance_known": False,
                "status": "In scansione",
                "connected": False,
                "scan": {"state": "In scansione", "receive": {}, "change": {}},
            }
        ],
        "btc_security": [],
        "btc_price_usd": 50,
        "fx_thb": 1,
        "gas_usd": 0,
        "eth_price_usd": 1,
        "node": {"configured": True, "connected": True, "host": "10.21.21.10", "port": 50001, "detail": "ok"},
        "alternatives": [],
        "evm_owners": [],
        "wallet_count": 1,
        "ens_names": [],
        "ens_alerts": [],
        "saved": True,
        "sources": {},
    }
    bitcoin.remember_scans(
        [
            {
                "id": 1,
                "owner": "io",
                "label": "Bitkey",
                "kind": "multisig",
                "btc": 0.2,
                "balance_known": True,
                "complete": True,
                "stale": False,
                "status": "Sincronizzato",
                "connected": True,
                "scanned_at": 1_700_000_100,
                "scan": {
                    "state": "Sincronizzato",
                    "receive": {"scanned": 20, "last_used": 8, "first_address": "bc1qexample"},
                    "change": {"scanned": 20, "last_used": 2, "first_address": "bc1qchange"},
                    "utxo_count": 3,
                    "confirmed_sats": 20_000_000,
                    "unconfirmed_sats": 0,
                    "error": "",
                    "complete": True,
                },
            }
        ],
        {"configured": True, "connected": True, "host": "10.21.21.10", "port": 50001, "height": 100, "detail": "Collegato via 10.21.21.10:50001."},
    )
    view = asyncio.run(portfolio.snapshot("tutti"))
    assert view["btc_wallets"][0]["btc"] == pytest.approx(0.2)
    assert view["btc_wallets"][0]["scan"]["receive"]["first_address"] == "bc1qexample"
    assert view["btc_wallets"][0]["balance_known"] is True
