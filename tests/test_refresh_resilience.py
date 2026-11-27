"""0.7.3: a slow or failing reader never blanks the dashboard or blocks the request."""
from __future__ import annotations

import asyncio

import pytest

from app import db, ens, evm, markets, portfolio, switches

WALLETS = [("Esempio", "0x" + "11" * 20, "io"), ("Casa", "0x" + "22" * 20, "moglie")]


def _row(label, address, owner, usd):
    return {
        "owner": owner, "label": label, "address": address, "chain": "ethereum",
        "protocol": "Lido", "asset": "stETH", "amount": usd / 2000, "token": "x",
        "price_token": "p", "valuation_ok": True, "usd": usd, "price_usd": 2000, "apy": 2.5, "apy30": 2.5,
    }


@pytest.fixture
def wallets(monkeypatch):
    db.init_db()
    for row in db.list_evm():
        db.delete_evm(row["id"])
    for label, address, owner in WALLETS:
        db.add_evm(label, address, owner)
    previous = {
        "ts": 1, "rpc_ok": True, "errors": [], "fx_thb": 34.0, "gas_usd": 1.0, "eth_price_usd": 2000.0,
        "positions": [_row(label, address, owner, 1000.0) for label, address, owner in WALLETS],
        "switches": [], "switch_anomalies": [], "security": [], "alternatives": [], "evm_owners": ["io", "moglie"],
        "wallet_count": 2, "ens_names": [], "ens_alerts": [], "sources": {}, "saved": True,
    }
    monkeypatch.setattr(portfolio, "_SNAPSHOT", previous)
    monkeypatch.setattr(portfolio, "_SNAPSHOT_AT", 1.0)
    monkeypatch.setattr(portfolio, "_TASK", None)

    async def rpc(method, params, url=None, chain="ethereum"):
        return "0x1"

    async def enrich(rows):
        return [{**r, "usd": r.get("usd") if r.get("usd") is not None else 500.0, "apy": 2.5, "apy30": 2.5} for r in rows], []

    async def fx():
        return 34.0

    async def nothing(*args, **kwargs):
        return []

    async def gas(*args, **kwargs):
        return 1.0

    monkeypatch.setattr(evm, "eth_rpc", rpc)
    monkeypatch.setattr(portfolio, "_enrich", enrich)
    monkeypatch.setattr(markets, "fx_thb", fx)
    monkeypatch.setattr(markets, "protocol_health", nothing)
    monkeypatch.setattr(switches, "gas_usd_now", gas)
    monkeypatch.setattr(switches, "market_alternatives", nothing)
    monkeypatch.setattr(ens, "scan_wallets", nothing)
    return previous


def test_slow_read_answers_fast_with_the_last_snapshot(monkeypatch, wallets):
    async def slow_scan(address, owner, label, chains_ok=None):
        await asyncio.sleep(0.5)
        return [{**_row(label, address, owner, 0), "usd": None}], [], []

    monkeypatch.setattr(evm, "scan_wallet", slow_scan)
    monkeypatch.setattr(portfolio, "REFRESH_WAIT_SECONDS", 0.05)

    async def run():
        first = await portfolio.snapshot("tutti", force=True)
        assert first["refreshing"] is True
        assert first["totals"]["tutti"]["total_usd"] == pytest.approx(2000)
        await portfolio._TASK
        again = await portfolio.snapshot("tutti")
        assert again["refreshing"] is False
        assert again["totals"]["tutti"]["total_usd"] == pytest.approx(1000)

    asyncio.run(run())


def test_no_balances_read_keeps_the_previous_snapshot(monkeypatch, wallets):
    async def broken(address, owner, label, chains_ok=None):
        raise RuntimeError("HTTP 429")

    monkeypatch.setattr(evm, "scan_wallet", broken)
    data = asyncio.run(portfolio.snapshot("tutti", force=True))
    assert data["totals"]["tutti"]["total_usd"] == pytest.approx(2000)
    assert data["kept_previous"] is True
    assert any("Resta l'ultimo saldo" in e for e in data["errors"])


def test_one_slow_wallet_keeps_its_last_rows(monkeypatch, wallets):
    async def scan(address, owner, label, chains_ok=None):
        if label == "Casa":
            await asyncio.sleep(5)
        return [{**_row(label, address, owner, 0), "usd": 1500.0}], [], []

    monkeypatch.setattr(evm, "scan_wallet", scan)
    monkeypatch.setattr(portfolio, "WALLET_TIMEOUT", 0.1)
    data = asyncio.run(portfolio.snapshot("tutti", force=True))
    by_label = {}
    for row in data["positions"]:
        by_label[row["label"]] = by_label.get(row["label"], 0) + row["usd"]
    assert by_label == {"Esempio": pytest.approx(1500), "Casa": pytest.approx(1000)}
    assert any("Casa" in e and "precedente" in e for e in data["errors"])


def test_aave_reader_failure_keeps_the_wallet_tokens(monkeypatch):
    async def bal(address, chain="ethereum"):
        return 2 * 10**18

    async def hang(*args, **kwargs):
        await asyncio.sleep(5)

    async def no_v4(*args, **kwargs):
        return [], [], []

    async def no_tokens(token, holder, chain="ethereum"):
        return 0

    monkeypatch.setattr(evm, "eth_get_balance", bal)
    monkeypatch.setattr(evm, "USE_MULTICALL", False)
    monkeypatch.setattr(evm, "token_balance", no_tokens)
    monkeypatch.setattr(evm, "scan_aave_v3", hang)
    monkeypatch.setattr(evm, "scan_aave_v4", no_v4)
    monkeypatch.setattr(evm, "AAVE_READ_TIMEOUT", 0.1)
    rows, notes, reports = asyncio.run(evm.scan_wallet("0x" + "33" * 20, "io", "Risparmio"))
    assert [r["asset"] for r in rows] == ["ETH"]
    assert any("Aave V3" in n for n in notes)


def test_a_long_read_never_falls_back_to_hundreds_of_single_calls(monkeypatch):
    singles = []

    async def refuse(*args, **kwargs):
        raise RuntimeError("HTTP 429")

    async def single(target, data, chain="ethereum", block=None):
        singles.append(target)
        return "0x" + "00" * 32

    monkeypatch.setattr(evm, "multicall", refuse)
    monkeypatch.setattr(evm, "eth_call_raw", single)
    calls = [("0x" + "44" * 20, "0x70a08231")] * 100
    with pytest.raises(RuntimeError):
        asyncio.run(evm._multicall_or_each(calls))
    assert singles == []
    short = asyncio.run(evm._multicall_or_each(calls[:3]))
    assert len(short) == 3 and len(singles) == 3
