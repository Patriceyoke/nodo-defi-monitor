"""Token map for read-only balances.

Every address below was checked with eth_call symbol() and decimals() on a
public RPC (Ethereum: ethereum.publicnode.com, BNB: bsc.publicnode.com).
Entries that reverted or returned empty data were dropped. ERC-4626 vaults
are valued with convertToAssets, not as 1 share = 1 underlying.
"""
from __future__ import annotations

# chain id -> public RPCs, tried in order. Add a chain by extending this dict
# and tagging tokens with that chain name.
CHAINS: dict[str, dict] = {
    "ethereum": {
        "label": "Ethereum",
        "llama": "ethereum",
        "rpcs": [
            "https://ethereum.publicnode.com",
            "https://cloudflare-eth.com",
            "https://rpc.mevblocker.io",
        ],
    },
    "bsc": {
        "label": "BNB Chain",
        "llama": "bsc",
        "rpcs": [
            "https://bsc.publicnode.com",
            "https://bsc-dataseed.binance.org",
            "https://1rpc.io/bnb",
        ],
    },
    # Extra chains are for the NFT section. scan_balances stays off so the DeFi
    # refresh does not probe them or warn when their RPC is quiet.
    "polygon": {
        "label": "Polygon",
        "llama": "polygon",
        "scan_balances": False,
        "rpcs": ["https://polygon-bor.publicnode.com"],
    },
    "arbitrum": {
        "label": "Arbitrum",
        "llama": "arbitrum",
        "scan_balances": False,
        "rpcs": ["https://arbitrum-one.publicnode.com"],
    },
    "base": {
        "label": "Base",
        "llama": "base",
        "scan_balances": False,
        "rpcs": ["https://base.publicnode.com"],
    },
}

WETH = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
STETH = "0xae7ab96520DE3A18E5e111B5EaAb095312D7fE84"
WSTETH = "0x7f39C581F595B53c5cb19bD0b3f8dA6c935E2Ca0"
RETH = "0xae78736Cd615f374D3085123A210448E74Fc6393"
USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
USDT = "0xdAC17F958D2ee523a2206206994597C13D831ec7"
DAI = "0x6B175474E89094C44Da98b954EedeAC495271d0F"
USDS = "0xdC035D45d973E3EC169d2276DDab16f1e407384F"
BSC_USDT = "0x55d398326f99059fF775485246999027B3197955"


def _erc20(
    *,
    chain: str,
    protocol: str,
    asset: str,
    symbol: str,
    token: str,
    decimals: int,
    price: str,
    llama_project: str | None = None,
    llama_symbol: str | None = None,
) -> dict:
    return {
        "chain": chain,
        "protocol": protocol,
        "asset": asset,
        "symbol": symbol,
        "token": token,
        "decimals": decimals,
        "kind": "erc20",
        "price": price,
        "price_chain": chain,
        "llama_project": llama_project,
        "llama_symbol": llama_symbol,
    }


def _erc4626(
    *,
    chain: str,
    protocol: str,
    asset: str,
    symbol: str,
    token: str,
    decimals: int,
    underlying: str,
    underlying_decimals: int,
    underlying_symbol: str,
    llama_project: str | None,
    llama_symbol: str | None,
    price_chain: str | None = None,
) -> dict:
    return {
        "chain": chain,
        "protocol": protocol,
        "asset": asset,
        "symbol": symbol,
        "token": token,
        "decimals": decimals,
        "kind": "erc4626",
        "underlying": underlying,
        "underlying_decimals": underlying_decimals,
        "underlying_symbol": underlying_symbol,
        "price": underlying,
        "price_chain": price_chain or chain,
        "llama_project": llama_project,
        "llama_symbol": llama_symbol,
    }


# Order is the scan order. kind erc20 means balanceOf is already the amount
# to price (wallet tokens, rebasing stETH, Aave aTokens, Compound v3 base).
TOKENS: list[dict] = [
    _erc20(
        chain="ethereum", protocol="Lido", asset="stETH", symbol="stETH",
        token=STETH, decimals=18, price=STETH,
        llama_project="lido", llama_symbol="STETH",
    ),
    _erc20(
        chain="ethereum", protocol="Lido", asset="wstETH", symbol="wstETH",
        token=WSTETH, decimals=18, price=WSTETH,
        # Holding wstETH earns the Lido staking rate. Llama has no separate WSTETH pool.
        llama_project="lido", llama_symbol="STETH",
    ),
    _erc20(
        chain="ethereum", protocol="Rocket Pool", asset="rETH", symbol="rETH",
        token=RETH, decimals=18, price=RETH,
        llama_project="rocket-pool", llama_symbol="RETH",
    ),
    _erc20(
        chain="ethereum", protocol="Aave", asset="USDC", symbol="aEthUSDC",
        token="0x98C23E9d8f34FEFb1B7BD6a91B7FF122F4e16F5c", decimals=6, price=USDC,
        llama_project="aave-v3", llama_symbol="USDC",
    ),
    _erc20(
        chain="ethereum", protocol="Aave", asset="USDT", symbol="aEthUSDT",
        token="0x23878914EFE38d27C4D67Ab83ed1b93A74D4086a", decimals=6, price=USDT,
        llama_project="aave-v3", llama_symbol="USDT",
    ),
    _erc20(
        chain="ethereum", protocol="Aave", asset="WETH", symbol="aEthWETH",
        token="0x4d5F47FA6A74757f35C14fD3a6Ef8E3C9BC514E8", decimals=18, price=WETH,
        llama_project="aave-v3", llama_symbol="WETH",
    ),
    _erc20(
        chain="ethereum", protocol="Compound", asset="USDC", symbol="cUSDCv3",
        token="0xc3d688B66703497DAA19211EEdff47f25384cdc3", decimals=6, price=USDC,
        llama_project="compound-v3", llama_symbol="USDC",
    ),
    _erc20(
        chain="ethereum", protocol="Compound", asset="USDT", symbol="cUSDTv3",
        token="0x3Afdc9BCA9213A35503b077a6072F3D0d5AB0840", decimals=6, price=USDT,
        llama_project="compound-v3", llama_symbol="USDT",
    ),
    _erc20(
        chain="ethereum", protocol="Compound", asset="WETH", symbol="cWETHv3",
        token="0xA17581A9E3356d9A858b789D68B4d866e593aE94", decimals=18, price=WETH,
        # The base-asset supply market is listed as ETH, not the WETH collateral rows.
        llama_project="compound-v3", llama_symbol="ETH",
    ),
    _erc4626(
        chain="ethereum", protocol="Fluid", asset="USDC", symbol="fUSDC",
        token="0x9Fb7b4477576Fe5B32be4C1843aFB1e55F251B33", decimals=6,
        underlying=USDC, underlying_decimals=6, underlying_symbol="USDC",
        llama_project="fluid-lending", llama_symbol="USDC",
    ),
    _erc4626(
        chain="ethereum", protocol="Fluid", asset="USDT", symbol="fUSDT",
        token="0x5C20B550819128074FD538Edf79791733ccEdd18", decimals=6,
        underlying=USDT, underlying_decimals=6, underlying_symbol="USDT",
        llama_project="fluid-lending", llama_symbol="USDT",
    ),
    _erc4626(
        chain="ethereum", protocol="Sky", asset="sUSDS", symbol="sUSDS",
        token="0xa3931d71877C0E7a3148CB7Eb4463524FEc27fbD", decimals=18,
        underlying=USDS, underlying_decimals=18, underlying_symbol="USDS",
        llama_project="sky-lending", llama_symbol="SUSDS",
    ),
    _erc4626(
        chain="ethereum", protocol="Sky", asset="stUSDS", symbol="stUSDS",
        token="0x99CD4Ec3f88A45940936F469E4bB72A2A701EEB9", decimals=18,
        underlying=USDS, underlying_decimals=18, underlying_symbol="USDS",
        llama_project="sky-lending", llama_symbol="STUSDS",
    ),
    _erc4626(
        chain="ethereum", protocol="Spark", asset="USDC", symbol="spUSDC",
        token="0x28B3a8fb53B741A8Fd78c0fb9A6B2393d896a43d", decimals=6,
        underlying=USDC, underlying_decimals=6, underlying_symbol="USDC",
        llama_project="spark-savings", llama_symbol="USDC",
    ),
    _erc4626(
        chain="ethereum", protocol="Spark", asset="USDT", symbol="spUSDT",
        token="0xe2e7a17dFf93280dec073C995595155283e3C372", decimals=6,
        underlying=USDT, underlying_decimals=6, underlying_symbol="USDT",
        llama_project="spark-savings", llama_symbol="USDT",
    ),
    # SparkLend spTokens are Aave-style aTokens: balanceOf is already underlying.
    _erc20(
        chain="ethereum", protocol="SparkLend", asset="USDC", symbol="spUSDC",
        token="0x377C3bd93f2a2984E1E7bE6A5C22c525eD4A4815", decimals=6, price=USDC,
        llama_project="sparklend", llama_symbol="USDC",
    ),
    _erc20(
        chain="ethereum", protocol="SparkLend", asset="USDT", symbol="spUSDT",
        token="0xe7dF13b8e3d6740fe17CBE928C7334243d86c92f", decimals=6, price=USDT,
        llama_project="sparklend", llama_symbol="USDT",
    ),
    _erc4626(
        chain="ethereum", protocol="Maple", asset="USDC", symbol="syrupUSDC",
        token="0x80ac24aA929eaF5013f6436cdA2a7ba190f5Cc0b", decimals=6,
        underlying=USDC, underlying_decimals=6, underlying_symbol="USDC",
        llama_project="maple", llama_symbol="USDC",
    ),
    _erc4626(
        chain="ethereum", protocol="Maple", asset="USDT", symbol="syrupUSDT",
        token="0x356B8d89c1e1239Cbbb9dE4815c39A1474d5BA7D", decimals=6,
        underlying=USDT, underlying_decimals=6, underlying_symbol="USDT",
        llama_project="maple", llama_symbol="USDT",
    ),
    _erc20(
        chain="ethereum", protocol="Wallet", asset="USDC", symbol="USDC",
        token=USDC, decimals=6, price=USDC,
    ),
    _erc20(
        chain="ethereum", protocol="Wallet", asset="USDT", symbol="USDT",
        token=USDT, decimals=6, price=USDT,
    ),
    _erc20(
        chain="ethereum", protocol="Wallet", asset="DAI", symbol="DAI",
        token=DAI, decimals=18, price=DAI,
    ),
    _erc20(
        chain="ethereum", protocol="Wallet", asset="USDS", symbol="USDS",
        token=USDS, decimals=18, price=USDS,
    ),
    _erc20(
        chain="ethereum", protocol="Wallet", asset="WETH", symbol="WETH",
        token=WETH, decimals=18, price=WETH,
    ),
    # Own protocol line so it is not merged with Ethereum Fluid USDT.
    _erc4626(
        chain="bsc", protocol="Fluid BNB", asset="USDT", symbol="fUSDT",
        token="0xA5b8FCa32E5252B0B58EAbf1A8c79d958F8EE6A2", decimals=18,
        underlying=BSC_USDT, underlying_decimals=18, underlying_symbol="USDT",
        llama_project=None, llama_symbol=None,
    ),
]
