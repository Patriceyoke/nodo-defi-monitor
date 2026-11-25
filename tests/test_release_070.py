"""0.7.0: batched reads, RPC fallback, hourly net worth, compact Hermes tools."""
from __future__ import annotations

import asyncio
import json
import time

import httpx

from app import compact, db, evm
from app.mcp_server import call_tool


def _pack(items: list[tuple[bool, bytes]]) -> str:
    blobs = []
    for success, payload in items:
        pad = (32 - len(payload) % 32) % 32
        blob = (1 if success else 0).to_bytes(32, "big")
        blob += (64).to_bytes(32, "big")
        blob += len(payload).to_bytes(32, "big")
        blob += payload + b"\x00" * pad
        blobs.append(blob)
    cursor = len(blobs) * 32
    offsets = []
    for blob in blobs:
        offsets.append(cursor)
        cursor += len(blob)
    body = len(blobs).to_bytes(32, "big")
    body += b"".join(item.to_bytes(32, "big") for item in offsets)
    body += b"".join(blobs)
    return "0x" + ((32).to_bytes(32, "big") + body).hex()


def test_multicall_allow_failure_keeps_the_other_reads():
    target = "0x" + "ab" * 20
    encoded = evm.encode_aggregate3([(target, "0x70a08231" + "11" * 32)])
    body = bytes.fromhex(encoded[10:])
    offset = int.from_bytes(body[0:32], "big")
    first = int.from_bytes(body[offset + 32:offset + 64], "big")
    start = offset + 32 + first
    allow = int.from_bytes(body[start + 32:start + 64], "big")
    assert allow == 1

    good = (5).to_bytes(32, "big")
    decoded = evm.decode_aggregate3(_pack([(True, good), (False, b"")]))
    specs = [
        {"chain": "ethereum", "token": "0x" + "11" * 20, "protocol": "Aave", "asset": "USDC"},
        {"chain": "ethereum", "token": "0x" + "22" * 20, "protocol": "Fluid", "asset": "USDT"},
    ]
    balances, failed = evm.apply_multicall_reads(specs, decoded)
    assert balances[("ethereum", specs[0]["token"])] == 5
    assert [row["protocol"] for row in failed] == ["Fluid"]


def _client(handler):
    return lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_rpc_falls_back_on_429_and_skips_the_dead_endpoint(monkeypatch):
    evm.reset_rpc_circuit()
    monkeypatch.setattr(evm, "RPC_BACKOFF_SECONDS", 0)
    monkeypatch.setattr(evm, "rpc_candidates", lambda chain: ["https://dead.example/rpc", "https://ok.example/rpc"])
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if "dead" in str(request.url):
            return httpx.Response(429, json={"jsonrpc": "2.0", "id": 1, "error": "slow"})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "0x10"})

    monkeypatch.setattr(evm, "_http_client", _client(handler))
    assert asyncio.run(evm.eth_rpc("eth_blockNumber", [], chain="ethereum")) == "0x10"
    assert any("dead" in url for url in seen)
    seen.clear()
    assert asyncio.run(evm.eth_rpc("eth_blockNumber", [], chain="ethereum")) == "0x10"
    assert seen == ["https://ok.example/rpc"]


def test_rpc_falls_back_on_timeout(monkeypatch):
    evm.reset_rpc_circuit()
    monkeypatch.setattr(evm, "RPC_BACKOFF_SECONDS", 0)
    monkeypatch.setattr(evm, "rpc_candidates", lambda chain: ["https://slow.example/rpc", "https://ok.example/rpc"])

    def handler(request: httpx.Request) -> httpx.Response:
        if "slow" in str(request.url):
            raise httpx.TimeoutException("timeout")
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "0x2"})

    monkeypatch.setattr(evm, "_http_client", _client(handler))
    assert asyncio.run(evm.eth_rpc("eth_call", [{"to": "0x" + "11" * 20, "data": "0x"}, "latest"], chain="ethereum")) == "0x2"


def test_snapshot_is_hourly_pruned_and_readable(client):
    db.init_db()
    now = int(time.time())
    row = {
        "owner": "combinato",
        "wallet": "Demo070",
        "net_worth_usd": 10,
        "allocation": [{"protocol": "Aave", "asset": "USDC", "usd": 10, "share": 1}],
        "apy": 4.5,
        "health_factor": None,
    }
    assert db.save_portfolio_snapshots([row], ts=now, min_interval=3600) == 1
    assert db.save_portfolio_snapshots([dict(row, net_worth_usd=11)], ts=now + 10, min_interval=3600) == 0
    assert db.save_portfolio_snapshots([dict(row, net_worth_usd=12)], ts=now + 3600, min_interval=3600) == 1
    old = dict(row, wallet="DemoVecchio", net_worth_usd=1)
    db.save_portfolio_snapshots([old], ts=now - 800 * 86400, min_interval=0)
    db.save_portfolio_snapshots([dict(row, net_worth_usd=13)], ts=now + 7200, min_interval=0)
    assert db.portfolio_history("combinato", wallet="DemoVecchio", days=900, now=now + 7200) == []
    res = client.get("/api/history", params={"owner": "combinato", "wallet": "Demo070", "days": 30})
    assert res.status_code == 200
    body = res.json()
    assert body["owner"] == "combinato"
    assert body["wallet"] == "Demo070"
    assert [point["net_worth_usd"] for point in body["points"]][-1] == 13
    assert "0x" not in json.dumps(body["points"])


def _positions():
    return [
        {
            "owner": "io",
            "label": "Demo Jade",
            "address": "0x" + "ab" * 20,
            "protocol": "Aave",
            "asset": "WETH",
            "usd": 1000,
            "apy": 3,
            "collateral_usd": 1000,
            "debt_usd": 400,
            "debt_asset": "USDC",
            "health_factor": 2.0,
            "liquidation_threshold": 0.8,
        },
        {
            "owner": "io",
            "label": "Demo Jade",
            "address": "0x" + "cd" * 20,
            "protocol": "Compound",
            "asset": "USDC",
            "usd": 50,
            "apy": 5,
            "price_usd": 0.9,
        },
        {"owner": "moglie", "label": "Demo N1", "protocol": "Lido", "asset": "wstETH", "usd": 20, "apy": 3},
        {"owner": "moglie", "label": "Demo N1", "protocol": "Spark", "asset": "DAI", "usd": 15, "apy": 4},
        {"owner": "moglie", "label": "Demo N1", "protocol": "Sky", "asset": "USDS", "usd": 12, "apy": 6},
        {"owner": "moglie", "label": "Demo N1", "protocol": "Fluid", "asset": "USDT", "usd": 8, "apy": 7},
        {"owner": "moglie", "label": "Demo N1", "protocol": "Maple", "asset": "USDC", "usd": 4, "apy": 8},
    ]


def test_compact_summary_risk_and_shock():
    rows = _positions()
    summary = compact.portfolio_summary(rows, "combinato")
    assert len([line for line in summary["lines"] if line["protocol"] != "altro"]) == 5
    assert summary["lines"][-1]["protocol"] == "altro"
    assert summary["total_usd"] == 1109
    assert "0x" not in summary["text"]

    quiet = compact.risk_and_alerts(
        [{"label": "Demo", "protocol": "Aave", "asset": "USDC", "health_factor": 2.2, "price_usd": 1}],
        [],
    )
    assert quiet["text"] == "Nessun rischio"

    risky = compact.risk_and_alerts(
        [{
            "label": "Demo Jade",
            "address": "0x" + "11" * 20,
            "protocol": "Aave",
            "asset": "WETH",
            "health_factor": 1.5,
            "claimable_usd": 9,
        }],
        ["RPC ethereum non raggiungibile"],
    )
    assert any(item["kind"] == "health" for item in risky["items"])
    assert any(item["kind"] == "reward" for item in risky["items"])
    assert any(item["kind"] == "error" for item in risky["items"])
    depeg = compact.risk_and_alerts(rows, [])
    assert any(item["kind"] == "depeg" for item in depeg["items"])
    dumped = json.dumps(compact.without_addresses(risky))
    assert "0x" + "11" * 20 not in dumped

    shock = compact.simulate_market_shock(rows[:1], "WETH", -50)
    assert shock["value_before_usd"] == 1000
    assert shock["value_after_usd"] == 500
    assert shock["value_delta_usd"] == -500
    assert shock["positions"][0]["health_factor_after"] == 1.0
    assert shock["positions"][0]["liquidation_room_pct"] == 0
    debt = compact.simulate_market_shock(rows[:1], "USDC", -50)
    assert debt["value_delta_usd"] == 0
    assert debt["positions"][0]["health_factor_after"] == 4.0


def test_mcp_compact_tools_use_labels_only(monkeypatch):
    from app import mcp_server

    monkeypatch.setattr(mcp_server, "cached_snapshot", lambda: {"positions": _positions(), "errors": []})
    summary = asyncio.run(call_tool("get_portfolio_summary", {"owner": "io", "wallet": "Demo Jade"}))
    text = summary["content"][0]["text"]
    assert "Demo Jade" in text
    assert "0x" not in text
    risk = asyncio.run(call_tool("get_risk_and_alerts", {}))
    assert "Nessun rischio" not in risk["content"][0]["text"]
    shock = asyncio.run(call_tool("simulate_market_shock", {"asset": "WETH", "pct_change": -50}))
    payload = json.loads(shock["content"][0]["text"])
    assert payload["value_delta_usd"] == -500
    assert payload["positions"][0]["health_factor_after"] == 1.0
    assert "0x" not in shock["content"][0]["text"]
    monkeypatch.setattr(mcp_server, "cached_snapshot", lambda: {"positions": [], "errors": []})
    empty = asyncio.run(call_tool("get_risk_and_alerts", {}))
    assert "Nessun rischio" in empty["content"][0]["text"]
