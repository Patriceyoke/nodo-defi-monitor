"""Add a wallet, force a refresh against a mock RPC, and read the result."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import config, db, evm, markets, portfolio
from app.main import app
from app.protocols import WETH


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


def _install_mocks(monkeypatch):
    async def fake_rpc(method, params, url=None, chain="ethereum"):
        if method == "eth_blockNumber":
            evm._remember_rpc(chain, "http://mock-rpc", True, None)
            return "0x10"
        if method == "eth_getBalance":
            return hex(10**18)  # 1 ETH
        if method == "eth_gasPrice":
            return "0x3b9aca00"
        return "0x0"

    async def fake_prices(tokens):
        return {"ETH": 2500.0, f"ethereum:{WETH.lower()}": 2500.0}

    async def fake_btc():
        return None

    async def fake_fx():
        return 1.0

    async def fake_pools():
        return []

    async def fake_health():
        return []

    monkeypatch.setattr(evm, "eth_rpc", fake_rpc)
    monkeypatch.setattr(markets, "prices_usd", fake_prices)
    monkeypatch.setattr(markets, "btc_price_usd", fake_btc)
    monkeypatch.setattr(markets, "fx_thb", fake_fx)
    monkeypatch.setattr(markets, "llama_pools", fake_pools)
    monkeypatch.setattr(markets, "protocol_health", fake_health)


def test_alerts_and_debug_open_before_any_refresh(isolated_db):
    client = TestClient(app)
    alerts = client.get("/api/alerts")
    assert alerts.status_code == 200
    body = alerts.json()
    assert "errors" in body
    assert body["ok"] is True
    status = client.get("/api/debug/refresh-status")
    assert status.status_code == 200
    assert "last_finished_at" in status.json()
    index = client.get("/api")
    assert index.status_code == 200
    routes = index.json()["routes"]
    assert "GET /api/alerts" in routes
    assert "GET /api/debug/refresh-status" in routes
    assert "POST /api/refresh?owner=tutti" in routes


def test_add_wallet_refresh_portfolio_is_not_empty(isolated_db, monkeypatch):
    _install_mocks(monkeypatch)
    client = TestClient(app)
    created = client.post(
        "/api/wallets/evm",
        json={"label": "Prova", "address": "0x000000000000000000000000000000000000dEaD", "owner": "io"},
    )
    assert created.status_code == 200, created.text
    refreshed = client.post("/api/refresh?owner=tutti")
    assert refreshed.status_code == 200, refreshed.text
    body = refreshed.json()
    assert body["positions"], body.get("errors")
    assert any(row["asset"] == "ETH" and row["owner"] == "io" for row in body["positions"])
    assert body["totals"]["tutti"]["total_usd"] > 0
    assert body["wallet_count"] >= 1

    status = client.get("/api/debug/refresh-status").json()
    assert status["last_finished_at"]
    assert status["last_ok"] is True
    assert status["position_count"] >= 1
    assert status["rpc"]["ethereum"]["url"] == "http://mock-rpc"
    assert status["wallets"][0]["address"].lower().endswith("dead")
    assert status["wallets"][0]["positions"] >= 1

    alerts = client.get("/api/alerts")
    assert alerts.status_code == 200
    assert "errors" in alerts.json()
