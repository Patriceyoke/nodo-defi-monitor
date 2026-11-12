"""Token map integrity and ERC-4626 valuation."""
from __future__ import annotations

import asyncio

import pytest

from app import evm, portfolio
from app.protocols import BSC_USDT, CHAINS, TOKENS, USDC
from tests.eip55 import keccak256, to_checksum

# Public entities, not personal wallets: Curve stETH pool, Lido treasury,
# Binance hot wallets, and the Aave Collector contract.
PUBLIC_WHALES = [
    ("Lido", "stETH", "0xDC24316b9AE028F1497c275EB9192a3Ea0f67022"),
    ("Lido", "wstETH", "0x3e40D73EB977Dc6a537aF587D48316feE66E9C8c"),
    ("Wallet", "USDC", "0x28C6c06298d514Db089934071355E5743bf21d60"),
    ("Wallet", "USDT", "0x28C6c06298d514Db089934071355E5743bf21d60"),
    ("Wallet", "DAI", "0x28C6c06298d514Db089934071355E5743bf21d60"),
    ("Wallet", "WETH", "0xF977814e90dA44bFA03b6295A0616a897441aceC"),
    ("Aave", "USDC", "0x464C71f6c2F760DdA6093dCB91C24c39e5d6e18c"),
    ("Aave", "USDT", "0x464C71f6c2F760DdA6093dCB91C24c39e5d6e18c"),
    ("Aave", "WETH", "0x464C71f6c2F760DdA6093dCB91C24c39e5d6e18c"),
]

# These were in the previous map and do not answer symbol()/decimals().
DROPPED = {
    "0x3Afdc9BCA9213A1828736B60d22d32543dB36947",
    "0x9Fb7b4477576Fe5B32be4C1843aFB1e55F2683B9",
    "0x5C20B550819128746FD398D1Cbb3B8e2C4A33761",
    "0x0B925eD163218f6662a35e0f5a4153E95D62BF27",
    "0x90551A917898c13c8399516435872bf44D37B07d",
}


def test_keccak_empty_vector():
    assert keccak256(b"").hex() == "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"


def test_token_map_addresses_are_checksummed_and_complete():
    assert set(CHAINS) >= {"ethereum", "bsc"}
    present = {(row["protocol"], row["asset"], row["chain"]) for row in TOKENS}
    expected = {
        ("Lido", "stETH", "ethereum"),
        ("Lido", "wstETH", "ethereum"),
        ("Rocket Pool", "rETH", "ethereum"),
        ("Aave", "USDC", "ethereum"),
        ("Aave", "USDT", "ethereum"),
        ("Aave", "WETH", "ethereum"),
        ("Compound", "USDC", "ethereum"),
        ("Compound", "USDT", "ethereum"),
        ("Compound", "WETH", "ethereum"),
        ("Fluid", "USDC", "ethereum"),
        ("Fluid", "USDT", "ethereum"),
        ("Sky", "sUSDS", "ethereum"),
        ("Sky", "stUSDS", "ethereum"),
        ("Spark", "USDC", "ethereum"),
        ("Spark", "USDT", "ethereum"),
        ("SparkLend", "USDC", "ethereum"),
        ("SparkLend", "USDT", "ethereum"),
        ("Maple", "USDC", "ethereum"),
        ("Maple", "USDT", "ethereum"),
        ("Wallet", "USDC", "ethereum"),
        ("Wallet", "USDT", "ethereum"),
        ("Wallet", "DAI", "ethereum"),
        ("Wallet", "USDS", "ethereum"),
        ("Wallet", "WETH", "ethereum"),
        ("Fluid BNB", "USDT", "bsc"),
    }
    assert expected <= present
    seen = set()
    for row in TOKENS:
        assert row["chain"] in CHAINS
        for field in ("token", "price"):
            addr = row[field]
            assert addr == to_checksum(addr), addr
            assert addr not in DROPPED
        if row["kind"] == "erc4626":
            assert row["underlying"] == to_checksum(row["underlying"])
            assert row["underlying_decimals"] > 0
        else:
            assert row["kind"] == "erc20"
        seen.add(row["token"].lower())
    assert len(seen) == len(TOKENS)
    bnb = next(row for row in TOKENS if row["chain"] == "bsc")
    assert bnb["symbol"] == "fUSDT"
    assert bnb["underlying"] == BSC_USDT
    assert bnb["decimals"] == 18


def test_erc4626_valuation_uses_convert_to_assets(monkeypatch):
    syrup = next(row["token"] for row in TOKENS if row["symbol"] == "syrupUSDC")

    async def fake_eth_balance(address, chain="ethereum"):
        return 0

    async def fake_token_balance(token, holder, chain="ethereum"):
        if token == syrup:
            return 2_000_000  # 2.0 shares, 6 decimals
        return 0

    async def fake_convert(token, shares, chain="ethereum"):
        assert token == syrup
        assert shares == 2_000_000
        assert chain == "ethereum"
        return 2_400_000  # 2.4 USDC, not 2.0

    monkeypatch.setattr(evm, "eth_get_balance", fake_eth_balance)
    monkeypatch.setattr(evm, "token_balance", fake_token_balance)
    monkeypatch.setattr(evm, "convert_to_assets", fake_convert)

    rows, notes, _reports = asyncio.run(evm.scan_wallet("0x" + "11" * 20, "io", "test"))
    hit = [row for row in rows if row["token"] == syrup]
    assert notes == []
    assert len(hit) == 1
    assert hit[0]["amount"] == pytest.approx(2.4)
    assert hit[0]["price_token"] == f"ethereum:{USDC}"
    assert hit[0]["protocol"] == "Maple"


def test_failed_conversion_keeps_the_position(monkeypatch):
    fluid = next(row["token"] for row in TOKENS if row["protocol"] == "Fluid" and row["asset"] == "USDC")

    async def fake_eth_balance(address, chain="ethereum"):
        return 0

    async def fake_token_balance(token, holder, chain="ethereum"):
        return 5_000_000 if token == fluid else 0

    async def fake_convert(token, shares, chain="ethereum"):
        raise RuntimeError("rpc down")

    monkeypatch.setattr(evm, "eth_get_balance", fake_eth_balance)
    monkeypatch.setattr(evm, "token_balance", fake_token_balance)
    monkeypatch.setattr(evm, "convert_to_assets", fake_convert)
    rows, notes, _reports = asyncio.run(evm.scan_wallet("0x" + "22" * 20, "io", "test"))
    hit = [row for row in rows if row["token"] == fluid]
    assert len(hit) == 1
    assert hit[0]["valuation_ok"] is False
    assert hit[0]["price_token"] == ""
    assert notes and "convertToAssets" in notes[0]


def test_missing_price_and_apy_do_not_hide_the_position(monkeypatch):
    async def no_prices(tokens):
        return {}

    async def no_apy(project, symbol, chain="ethereum"):
        return None, None, None

    monkeypatch.setattr(portfolio.markets, "prices_usd", no_prices)
    monkeypatch.setattr(portfolio.markets, "apy_for", no_apy)
    rows, alerts = asyncio.run(portfolio._enrich([
        {
            "owner": "io",
            "protocol": "Aave",
            "asset": "USDC",
            "amount": 1000,
            "token": USDC,
            "price_token": f"ethereum:{USDC}",
            "chain": "ethereum",
            "llama_project": "aave-v3",
            "llama_symbol": "USDC",
            "valuation_ok": True,
        }
    ]))
    assert len(rows) == 1
    assert rows[0]["usd"] is None
    assert rows[0]["apy"] is None
    assert any("Prezzo non disponibile" in item for item in alerts)
    assert any("APY non disponibile" in item for item in alerts)


def _decode_symbol(raw: str) -> str:
    if not raw or raw == "0x":
        return ""
    data = bytes.fromhex(raw[2:])
    if len(data) == 32:
        return data.rstrip(b"\x00").decode()
    length = int.from_bytes(data[32:64], "big")
    return data[64:64 + length].decode()


def test_public_whales_and_erc4626_rates():
    async def run():
        try:
            await evm.eth_rpc("eth_blockNumber", [], chain="ethereum")
        except Exception as exc:
            pytest.skip(f"Ethereum RPC non raggiungibile: {exc}")
        for protocol, asset, holder in PUBLIC_WHALES:
            spec = next(row for row in TOKENS if row["protocol"] == protocol and row["asset"] == asset and row["chain"] == "ethereum")
            raw = await evm.token_balance(spec["token"], holder, "ethereum")
            assert raw > 0, f"{protocol} {asset} balance of {holder} is zero"
        for spec in TOKENS:
            if spec["kind"] != "erc4626":
                continue
            one_share = 10 ** int(spec["decimals"])
            assets = await evm.convert_to_assets(spec["token"], one_share, spec["chain"])
            assert assets > 0
            # These vaults accrue above par. A 1:1 share price would mean the call was ignored.
            assert assets > one_share

    asyncio.run(run())
