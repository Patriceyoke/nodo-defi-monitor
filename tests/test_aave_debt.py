"""Aave V3 supplies and debt: collateral is read, debt is a negative line, income is net."""
from __future__ import annotations

import asyncio

import pytest

from app import compact, evm, flows, portfolio, switches
from app.protocols import AAVE_V3_POOLS, USDC, WSTETH

USER = "0x" + "11" * 20
A_WSTETH = "0x" + "a1" * 20
V_USDC = "0x" + "d1" * 20
A_USDC_STATIC = "0x98C23E9d8f34FEFb1B7BD6a91B7FF122F4e16F5c"


def _word(value: int) -> bytes:
    return int(value).to_bytes(32, "big")


def _addr(value: str) -> bytes:
    return bytes(12) + bytes.fromhex(value[2:])


def _reserve(decimals: int, liq_ray: int, var_ray: int, rid: int, a_token: str, v_debt: str) -> bytes:
    config = decimals << 48
    words = [
        _word(config), _word(10**27), _word(liq_ray), _word(10**27), _word(var_ray), _word(0),
        _word(0), _word(rid), _addr(a_token), bytes(32), _addr(v_debt), bytes(32), _word(0), _word(0), _word(0),
    ]
    return b"".join(words)


def test_ray_rate_is_compounded_per_second():
    # 4.22% APR on Aave is shown as about 4.31% APY.
    assert evm.ray_rate_to_apy_pct(int(0.0422 * 10**27)) == pytest.approx(4.31, abs=0.01)
    assert evm.ray_rate_to_apy_pct(0) == 0.0


def test_reserve_and_account_decode():
    item = evm.decode_v3_reserve_data(_reserve(18, 5, 7, 3, A_WSTETH, V_USDC))
    assert item["decimals"] == 18 and item["id"] == 3
    assert item["a_token"] == A_WSTETH.lower() and item["variable_debt"] == V_USDC.lower()
    assert item["stable_debt"] == ""
    account = evm.decode_account_data(
        _word(25_000 * 10**8) + _word(1_000 * 10**8) + _word(0) + _word(9_500) + _word(0) + _word(int(20.0 * 10**18))
    )
    assert account["debt_usd"] == pytest.approx(1000)
    assert account["health_factor"] == pytest.approx(20.0)


def _fake_chain(monkeypatch, *, static_usdc_supply: int = 0):
    core = AAVE_V3_POOLS[0]["address"]
    catalog = [
        {**evm.decode_v3_reserve_data(_reserve(18, int(0.0001 * 10**27), int(0.0005 * 10**27), 1, A_WSTETH, "0x" + "e1" * 20)),
         "underlying": WSTETH.lower()},
        {**evm.decode_v3_reserve_data(_reserve(6, int(0.03 * 10**27), int(0.0422 * 10**27), 2, A_USDC_STATIC, V_USDC)),
         "underlying": USDC.lower()},
    ]

    async def fake_catalog(pool):
        return list(catalog) if pool["address"] == core else []

    async def fake_calls(calls):
        out = []
        for target, data in calls:
            sel = data[2:10]
            if sel == evm.SELECTOR_V3_ACCOUNT_DATA:
                if target == core:
                    out.append((True, _word(25_000 * 10**8) + _word(1_000 * 10**8) + _word(0) + _word(9_500) + _word(0) + _word(int(20.0 * 10**18))))
                else:
                    out.append((True, bytes(192)))
            elif sel == evm.SELECTOR_V3_USER_CONFIG:
                # wstETH (id 1) collateral bit, USDC (id 2) borrowing bit.
                out.append((True, _word((1 << 3) | (1 << 4)) if target == core else bytes(32)))
            elif target.lower() == A_WSTETH.lower():
                out.append((True, _word(10 * 10**18)))
            elif target.lower() == V_USDC.lower():
                out.append((True, _word(1_000_000_000)))
            elif target.lower() == A_USDC_STATIC.lower():
                out.append((True, _word(static_usdc_supply)))
            else:
                out.append((True, _word(0)))
        return out

    async def fake_symbol(underlying):
        return {WSTETH.lower(): "wstETH", USDC.lower(): "USDC"}[underlying.lower()]

    monkeypatch.setattr(evm, "_v3_pool_catalog", fake_catalog)
    monkeypatch.setattr(evm, "_multicall_or_each", fake_calls)
    monkeypatch.setattr(evm, "_underlying_symbol", fake_symbol)


def test_scan_reads_collateral_and_debt(monkeypatch):
    _fake_chain(monkeypatch)
    rows, notes, _reports, locked = asyncio.run(
        evm.scan_aave_v3(USER, "io", "T2", {A_USDC_STATIC.lower()})
    )
    assert notes == []
    supply = [r for r in rows if not r.get("debt")]
    debt = [r for r in rows if r.get("debt")]
    assert len(supply) == 1 and supply[0]["protocol"] == "Aave" and supply[0]["asset"] == "wstETH"
    assert supply[0]["amount"] == pytest.approx(10.0)
    assert supply[0]["collateral_locked"] is True and supply[0]["lido_base"] is True
    assert supply[0]["price_token"] == f"ethereum:{WSTETH.lower()}"
    assert len(debt) == 1 and debt[0]["protocol"] == "Debito Aave" and debt[0]["asset"] == "USDC"
    assert debt[0]["amount"] == pytest.approx(1000.0)
    assert debt[0]["borrow_apy"] == pytest.approx(4.31, abs=0.01)
    assert debt[0]["health_factor"] == pytest.approx(20.0)
    assert debt[0]["collateral_assets"] == ["wstETH"]
    assert locked == {}


def test_static_atoken_is_not_counted_twice(monkeypatch):
    _fake_chain(monkeypatch, static_usdc_supply=1_000 * 10**6)
    rows, _notes, _reports, _locked = asyncio.run(
        evm.scan_aave_v3(USER, "io", "T2", {A_USDC_STATIC.lower()})
    )
    # The aEthUSDC supply stays with the static TOKENS row.
    assert not [r for r in rows if r["asset"] == "USDC" and not r.get("debt")]


def test_enrich_values_debt_negative_and_income_net(monkeypatch):
    async def prices(tokens):
        return {f"ethereum:{WSTETH.lower()}": 2_162.5, f"ethereum:{USDC.lower()}": 1.0, "ETH": 1_750.0}

    async def apy(project, symbol, chain="ethereum", meta=None):
        if project == "lido":
            return 2.5, 2.6, {"pool": "lido"}
        return None, None, None

    monkeypatch.setattr(portfolio.markets, "prices_usd", prices)
    monkeypatch.setattr(portfolio.markets, "apy_for", apy)
    rows = [
        {"owner": "io", "label": "T2", "chain": "ethereum", "protocol": "Aave", "asset": "wstETH", "amount": 10.0,
         "price_token": f"ethereum:{WSTETH.lower()}", "onchain_apy": 0.01, "lido_base": True,
         "llama_project": "aave-v3", "llama_symbol": "wstETH", "valuation_ok": True, "collateral_locked": True},
        {"owner": "io", "label": "T2", "chain": "ethereum", "protocol": "Debito Aave", "asset": "USDC", "amount": 1000,
         "price_token": f"ethereum:{USDC.lower()}", "debt": True, "borrow_apy": 4.31, "valuation_ok": True},
    ]
    out, _alerts = asyncio.run(portfolio._enrich(rows))
    coll = next(r for r in out if not r.get("debt"))
    debt = next(r for r in out if r.get("debt"))
    assert coll["usd"] == pytest.approx(10.0 * 2_162.5)
    assert coll["apy"] == pytest.approx(2.51)
    assert debt["usd"] == pytest.approx(-1000)
    assert debt["apy"] == pytest.approx(4.31)
    st = portfolio.stats(out)
    assert st["total_usd"] == pytest.approx(10.0 * 2_162.5 - 1000)
    expected_year = 10.0 * 2_162.5 * 0.0251 - 1000 * 0.0431
    assert st["year_usd"] == pytest.approx(expected_year)
    assert st["wapy"] == pytest.approx(expected_year / st["total_usd"] * 100)


def test_debt_and_locked_collateral_are_never_switched():
    assert switches.movable({"protocol": "Aave", "asset": "USDC"})
    assert not switches.movable({"protocol": "Debito Aave", "asset": "USDC", "debt": True})
    assert not switches.movable({"protocol": "Aave", "asset": "wstETH", "collateral_locked": True})


def test_debt_rows_are_not_outflows(monkeypatch):
    flows.db.init_db()
    seen = []
    monkeypatch.setattr(flows.db, "list_position_marks", lambda: [])
    monkeypatch.setattr(flows.db, "upsert_position_mark", lambda *args, **kwargs: seen.append(args[0]))
    flows.observe_positions([{"owner": "io", "protocol": "Debito Aave", "asset": "USDC", "amount": 1000, "usd": -1000, "debt": True, "address": USER}])
    assert seen == []


def test_shock_moves_health_with_the_collateral():
    rows = [{"label": "T2", "protocol": "Debito Aave", "asset": "USDC", "usd": -1000, "debt": True,
             "health_factor": 20.0, "debt_usd": 1000, "collateral_usd": 25_000, "debt_asset": "USDC",
             "collateral_assets": ["wstETH"]}]
    out = compact.simulate_market_shock(rows, "wstETH", -50)
    assert out["positions"][0]["health_factor_after"] == pytest.approx(20.0 / 2, rel=1e-3)
    assert out["positions"][0]["usd_after"] == pytest.approx(-1000)
