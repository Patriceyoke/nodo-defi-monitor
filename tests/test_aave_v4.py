"""Aave V4 supplies are spoke balances, not ERC-20 tokens."""
import asyncio

from app import evm, markets
from app.protocols import aave_v4_asset_symbol, aave_v4_hub_name

USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
CORE = "0xcca852bc40e560adc3b1cc58ca5b55638ce826c9"
MAIN = "0x94e7A5dCbE816e498b89aB752661904E2F56c485"
USER = "0x" + "11" * 20


def _word(value: int) -> bytes:
    return value.to_bytes(32, "big")


def _addr(value: str) -> bytes:
    return bytes.fromhex(value[2:].lower().rjust(64, "0"))


def _reserve(underlying: str, hub: str, decimals: int, offset: bool = False) -> bytes:
    words = [_addr(underlying), _addr(hub), _word(0), _word(decimals)]
    if offset:
        words = [_word(32), *words]
    return b"".join(words)


def test_decode_get_reserve_words_and_dynamic_offset():
    underlying, hub, decimals = evm.decode_aave_v4_reserve(_reserve(USDC, CORE, 6))
    assert underlying == USDC
    assert hub == CORE
    assert decimals == 6
    again = evm.decode_aave_v4_reserve("0x" + _reserve(USDC, CORE, 18, offset=True).hex())
    assert again == (USDC, CORE, 18)
    assert aave_v4_hub_name(CORE) == "Core"
    assert aave_v4_asset_symbol(USDC) == "USDC"


def test_catalog_and_scan_use_mocked_eth_calls(monkeypatch):
    evm._V4_CATALOG["rows"] = None
    evm._V4_CATALOG["at"] = 0
    monkeypatch.setattr(evm, "AAVE_V4_SPOKES", [{"name": "Main", "address": MAIN}])
    payload = _reserve(USDC, CORE, 6)
    seen: list[str] = []

    async def fake_eth_call(to, data, chain="ethereum"):
        seen.append(data[:10])
        assert to == MAIN
        assert data.startswith("0x99806546")
        return 1

    async def fake_raw(to, data, chain="ethereum"):
        seen.append(data[:10])
        if data.startswith("0x77778db3"):
            return "0x" + payload.hex()
        if data.startswith("0xf1568a89"):
            return "0x" + (15_038_000_000).to_bytes(32, "big").hex()
        raise AssertionError(data[:10])

    async def fail_multicall(*args, **kwargs):
        raise RuntimeError("multicall spento")

    monkeypatch.setattr(evm, "eth_call", fake_eth_call)
    monkeypatch.setattr(evm, "eth_call_raw", fake_raw)
    monkeypatch.setattr(evm, "multicall", fail_multicall)

    catalog = asyncio.run(evm.aave_v4_catalog())
    assert catalog[0]["spoke_name"] == "Main"
    assert catalog[0]["decimals"] == 6
    assert catalog[0]["underlying"] == USDC
    cached = asyncio.run(evm.aave_v4_catalog())
    assert cached == catalog
    assert seen.count("0x99806546") == 1

    evm._V4_CATALOG["rows"] = None
    rows, notes, reports = asyncio.run(evm.scan_aave_v4(USER, "io", "Esempio"))
    assert notes == []
    assert reports == []
    row = rows[0]
    assert row["protocol"] == "Aave V4"
    assert row["asset"] == "USDC"
    assert row["amount"] == 15038
    assert row["token"] == USDC
    assert row["llama_project"] == "aave-v4"
    assert row["llama_symbol"] == "USDC"
    assert row["llama_meta"] == "Core"
    assert row["market"] == "Core"
    assert row["valuation_ok"] is True
    assert row["label"] == "Esempio"
    assert row["address"] == USER


def test_zero_supply_is_skipped_and_unknown_symbol_is_read_on_chain(monkeypatch):
    unknown = "0x" + "ab" * 20
    evm._V4_SYMBOLS.pop(unknown, None)

    async def fake_catalog():
        return [
            {
                "spoke": MAIN,
                "spoke_name": "Main",
                "reserve_id": 3,
                "underlying": unknown,
                "hub": "0x" + "cd" * 20,
                "decimals": 8,
            },
            {
                "spoke": MAIN,
                "spoke_name": "Main",
                "reserve_id": 4,
                "underlying": USDC,
                "hub": CORE,
                "decimals": 6,
            },
        ]

    async def fake_raw(to, data, chain="ethereum"):
        if data.startswith("0x95d89b41"):
            # ABI dynamic string "NEW"
            body = b"NEW".ljust(32, b"\x00")
            return "0x" + (_word(32) + _word(3) + body).hex()
        if data.startswith("0xf1568a89") and data[10:74] == (3).to_bytes(32, "big").hex():
            return "0x" + (250_000_000).to_bytes(32, "big").hex()
        return "0x" + (0).to_bytes(32, "big").hex()

    async def fail_multicall(*args, **kwargs):
        raise RuntimeError("no")

    monkeypatch.setattr(evm, "aave_v4_catalog", fake_catalog)
    monkeypatch.setattr(evm, "eth_call_raw", fake_raw)
    monkeypatch.setattr(evm, "multicall", fail_multicall)
    rows, _notes, _reports = asyncio.run(evm.scan_aave_v4(USER, "io", "Esempio"))
    assert len(rows) == 1
    assert rows[0]["asset"] == "NEW"
    assert rows[0]["market"] == "Main"
    assert rows[0]["amount"] == 2.5


def test_scan_wallet_keeps_going_when_aave_v4_fails(monkeypatch):
    async def no_eth(*args, **kwargs):
        return 0

    async def no_balance(*args, **kwargs):
        return 0

    async def boom(*args, **kwargs):
        raise RuntimeError("rpc")

    monkeypatch.setattr(evm, "eth_get_balance", no_eth)
    monkeypatch.setattr(evm, "USE_MULTICALL", False)
    monkeypatch.setattr(evm, "token_balance", no_balance)
    monkeypatch.setattr(evm, "scan_aave_v4", boom)

    async def no_v3(*args, **kwargs):
        return [], [], [], {}

    monkeypatch.setattr(evm, "scan_aave_v3", no_v3)
    rows, notes, reports = asyncio.run(evm.scan_wallet(USER, "io", "Esempio"))
    assert rows == []
    assert any("Aave V4" in note for note in notes)
    assert reports[-1]["asset"] == "Aave V4"


def test_pool_meta_matches_the_named_hub_only(monkeypatch):
    pools = [
        {"chain": "Ethereum", "project": "aave-v4", "symbol": "USDC", "poolMeta": "Prime", "tvlUsd": 9_000_000_000, "apy": 3.0},
        {"chain": "Ethereum", "project": "aave-v4", "symbol": "USDC", "poolMeta": "Core", "tvlUsd": 500_000_000, "apy": 5.19},
        {"chain": "Ethereum", "project": "aave-v4", "symbol": "USDC", "poolMeta": "core extra", "tvlUsd": 800_000_000, "apy": 9.0},
        {"chain": "Ethereum", "project": "aave-v4", "symbol": "USDC", "poolMeta": "", "tvlUsd": 10, "apy": 99.0},
    ]

    async def fake_pools():
        return pools

    monkeypatch.setattr(markets, "llama_pools", fake_pools)
    chosen = asyncio.run(markets.match_pool("aave-v4", "USDC", "ethereum", "CORE"))
    assert chosen["apy"] == 5.19
    plain = asyncio.run(markets.match_pool("aave-v4", "USDC", "ethereum"))
    assert plain["apy"] == 99.0
    apy, _apy30, pool = asyncio.run(markets.apy_for("aave-v4", "USDC", "ethereum", "Core"))
    assert apy == 5.19
    assert pool["poolMeta"] == "Core"
