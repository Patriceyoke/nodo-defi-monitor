"""ENS expiry status, persisted dashboard cache, and lazy NFT cache."""
from __future__ import annotations

import asyncio
import time

import pytest

from app import db, ens, evm, nft, portfolio


DAY = 86400


def test_ens_status_thresholds():
    now = 1_700_000_000
    ok = ens.ens_status(now + 40 * DAY, now)
    soon = ens.ens_status(now + 10 * DAY, now)
    grace = ens.ens_status(now - 5 * DAY, now)
    dead = ens.ens_status(now - 100 * DAY, now)
    assert ok["level"] == "ok" and ok["in_grace"] is False
    assert soon["level"] == "warning" and soon["days_left"] == 10
    assert grace["level"] == "danger" and grace["in_grace"] is True
    assert dead["level"] == "danger" and dead["grace_ended"] is True
    alerts = ens.alerts_for(
        [
            {"owner": "io", "name": "io.eth", **soon},
            {"owner": "moglie", "name": "moglie.eth", **grace},
            {"owner": "io", "name": "lungo.eth", **ok},
        ]
    )
    assert [item["owner"] for item in alerts] == ["io", "moglie"]
    assert alerts[0]["level"] == "warning"
    assert alerts[1]["level"] == "danger"
    assert "grazia" in alerts[1]["text"]


def test_multicall_decode_roundtrip():
    value = 123456
    blob = (1).to_bytes(32, "big") + (64).to_bytes(32, "big") + (32).to_bytes(32, "big") + value.to_bytes(32, "big")
    body = (1).to_bytes(32, "big") + (32).to_bytes(32, "big") + blob
    raw = "0x" + ((32).to_bytes(32, "big") + body).hex()
    decoded = evm.decode_aggregate3(raw)
    assert decoded == [(True, value.to_bytes(32, "big"))]
    encoded = evm.encode_aggregate3([("0x" + "ab" * 20, "0x70a08231" + "11" * 32)])
    assert encoded.startswith("0x82ad56cb")


def test_cached_portfolio_does_not_scan(tmp_path, monkeypatch):
    from app import config

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "monitor.db")
    monkeypatch.setattr(config, "_READY", True)
    monkeypatch.setattr(config, "DATA_PERSISTENT", True)
    portfolio.invalidate()
    db.init_db()
    db.add_evm("Casa", "0x" + "ab" * 20, "io")
    saved = {
        "ts": 1_700_000_000,
        "rpc_ok": True,
        "errors": [],
        "positions": [
            {
                "owner": "io",
                "label": "Casa",
                "address": "0x" + "ab" * 20,
                "chain": "ethereum",
                "protocol": "Wallet",
                "asset": "ETH",
                "amount": 1,
                "usd": 2500,
                "apy": None,
            }
        ],
        "switches": [],
        "security": [],
        "btc_wallets": [],
        "btc_security": [],
        "btc_price_usd": None,
        "fx_thb": 35.0,
        "gas_usd": 0.0,
        "eth_price_usd": 2500.0,
        "node": {"configured": False, "connected": False, "detail": "test"},
        "alternatives": [],
        "evm_owners": ["io"],
        "wallet_count": 1,
        "ens_names": [],
        "ens_alerts": [
            {"owner": "io", "name": "casa.eth", "level": "warning", "text": "ENS casa.eth scade tra 9 giorni.", "days_left": 9},
            {"owner": "moglie", "name": "altro.eth", "level": "danger", "text": "ENS altro.eth è scaduto ed è nel periodo di grazia.", "in_grace": True},
        ],
    }
    db.cache_set("portfolio", saved)
    portfolio.invalidate()

    async def explode(*_args, **_kwargs):
        raise AssertionError("il portfolio in cache non deve rileggere la rete")

    monkeypatch.setattr(evm, "scan_wallet", explode)
    monkeypatch.setattr(ens, "scan_wallets", explode)
    view = asyncio.run(portfolio.snapshot("io", force=False))
    assert view["positions"][0]["asset"] == "ETH"
    assert view["ts"] == 1_700_000_000
    assert [item["name"] for item in view["ens_alerts"]] == ["casa.eth"]
    everyone = asyncio.run(portfolio.snapshot("tutti", force=False))
    assert len(everyone["ens_alerts"]) == 2
    cold = portfolio.invalidate()
    assert cold is None
    db.cache_delete_prefix("portfolio")
    portfolio.invalidate()
    empty = asyncio.run(portfolio.snapshot("tutti", force=False))
    assert empty["totals"]["tutti"]["total_usd"] == 0
    wallets = db.list_evm()
    db.init_db()
    assert db.list_evm() == wallets


def test_nft_section_uses_saved_cache(tmp_path, monkeypatch):
    from app import config

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "monitor.db")
    monkeypatch.setattr(config, "_READY", True)
    monkeypatch.setattr(config, "DATA_PERSISTENT", True)
    db.init_db()
    payload = {
        "owner": "tutti",
        "items": [{"name": "cached", "chain": "ethereum", "liquidity": False}],
        "errors": [],
        "cached_at": int(time.time()),
    }
    db.cache_set("nfts:tutti", payload)

    async def explode(*_args, **_kwargs):
        raise AssertionError("la cache NFT non deve interrogare Alchemy")

    monkeypatch.setattr(nft, "_alchemy_page", explode)
    got = asyncio.run(nft.collect_nfts("tutti", force=False))
    assert got["from_cache"] is True
    assert got["items"][0]["name"] == "cached"
