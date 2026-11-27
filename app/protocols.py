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

# Official Aave V4 Ethereum user spokes. Treasury is omitted.
# Checked against aave-dao/aave-address-book AaveV4EthereumSpokes.
AAVE_V4_SPOKES = [
    {"name": "Main", "address": "0x94e7A5dCbE816e498b89aB752661904E2F56c485"},
    {"name": "Bluechip", "address": "0x973a023A77420ba610f06b3858aD991Df6d85A08"},
    {"name": "Ethena Correlated", "address": "0x58131E79531caB1d52301228d1f7b842F26B9649"},
    {"name": "Ethena Ecosystem", "address": "0xba1B3D55D249692b669A164024A838309B7508AF"},
    {"name": "EtherFi", "address": "0xbF10BDfE177dE0336aFD7fcCF80A904E15386219"},
    {"name": "Forex", "address": "0xD8B93635b8C6d0fF98CbE90b5988E3F2d1Cd9da1"},
    {"name": "Gold", "address": "0x65407b940966954b23dfA3caA5C0702bB42984DC"},
    {"name": "Kelp", "address": "0x3131FE68C4722e726fe6B2819ED68e514395B9a4"},
    {"name": "Lido", "address": "0xe1900480ac69f0B296841Cd01cC37546d92F35Cd"},
    {"name": "Lombard BTC", "address": "0x7EC68b5695e803e98a21a9A05d744F28b0a7753D"},
]

# Hub address -> DefiLlama poolMeta. Checked against AaveV4EthereumHubs.
AAVE_V4_HUBS = {
    "0xcca852bc40e560adc3b1cc58ca5b55638ce826c9": "Core",
    "0x06002e9c4412cb7814a791ea3666d905871e536a": "Plus",
    "0x943827dca022d0f354a8a8c332da1e5eb9f9f931": "Prime",
    "0x62d63197660c080236193ca60b70e49a08e90368": "Global Dollar",
}

AAVE_V4_UNDERLYINGS = {
    "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2": "WETH",
    "0x7f39c581f595b53c5cb19bd0b3f8da6c935e2ca0": "wstETH",
    "0xcd5fe23c85820f7b72d0926fc9b05b43e359b7ee": "weETH",
    "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599": "WBTC",
    "0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf": "cbBTC",
    "0x7fc66500c84a76ad7e9c93437bfc5ac33e2ddae9": "AAVE",
    "0x514910771af9ca656af840dff83e8264ecf986ca": "LINK",
    "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48": "USDC",
    "0xdac17f958d2ee523a2206206994597c13d831ec7": "USDT",
    "0x1abaea1f7c830bd89acc67ec4af516284b1bc33c": "EURC",
    "0x8292bb45bf1ee4d140127049757c2e0ff06317ed": "RLUSD",
    "0xe343167631d89b6ffc58b88d6b7fb0228795491d": "USDG",
    "0xcacd6fd266af91b8aed52accc382b4e165586e29": "frxUSD",
    "0x40d16fc0246ad3160ccc09b8d0d3a2cd28ae6c2f": "GHO",
    "0x72dfb2e44f59c5ad2bafe84314e5b99a7cd5075e": "cirBTC",
    "0xae7ab96520de3a18e5e111b5eaab095312d7fe84": "stETH",
    "0x6b175474e89094c44da98b954eedeac495271d0f": "DAI",
    "0xdc035d45d973e3ec169d2276ddab16f1e407384f": "USDS",
}


def aave_v4_hub_name(hub: str) -> str:
    return AAVE_V4_HUBS.get((hub or "").lower(), "")


def aave_v4_asset_symbol(underlying: str) -> str | None:
    return AAVE_V4_UNDERLYINGS.get((underlying or "").lower())


# Aave V3-style lending pools on Ethereum. Every reserve is read from the pool
# itself (getReservesList + getReserveData), so new reserves need no code.
# Checked against aave-dao/aave-address-book (AaveV3Ethereum, AaveV3EthereumLido)
# and sparkdotfi/spark-address-registry (SparkLend Pool).
AAVE_V3_POOLS = [
    {"name": "Aave V3 Core", "protocol": "Aave", "debt_protocol": "Debito Aave",
     "address": "0x87870Bca3F3fD6335C3F4ce8392D69350B4fA4E2", "llama_project": "aave-v3"},
    {"name": "Aave V3 Prime", "protocol": "Aave", "debt_protocol": "Debito Aave",
     "address": "0x4e033931ad43597d96D6bcc25c280717730B58B1", "llama_project": "aave-v3"},
    {"name": "SparkLend", "protocol": "SparkLend", "debt_protocol": "Debito SparkLend",
     "address": "0xC13e21B648A5Ee794902342038FF3aDAB66BE987", "llama_project": "sparklend"},
]

# Collateral that also earns the Lido staking rate on top of the pool rate.
LIDO_UNDERLYINGS = {"steth", "wsteth"}
