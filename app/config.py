"""Runtime configuration. No personal data, no keys, no seeds."""
from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "Monitor DeFi"
APP_VERSION = "0.1.0"
DEFAULT_PORT = int(os.environ.get("PORT", "3847"))

_DEFAULT_DATA = Path(__file__).resolve().parents[1] / "data"
DATA_DIR = Path(os.environ.get("DATA_DIR", str(_DEFAULT_DATA)))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "monitor.db"

DEFAULT_RPC = os.environ.get("ETH_RPC_URL", "https://cloudflare-eth.com")
DEFAULT_ELECTRS_HOST = os.environ.get("ELECTRS_HOST", "")
DEFAULT_ELECTRS_PORT = int(os.environ.get("ELECTRS_PORT", "50001"))
DEFAULT_BTC_PRICE_URL = os.environ.get(
    "BTC_PRICE_URL",
    "https://api.coinbase.com/v2/prices/BTC-USD/spot",
)

# Fallback public Ethereum RPC list (no API keys required).
PUBLIC_RPCS = [
    "https://cloudflare-eth.com",
    "https://eth.llamarpc.com",
    "https://rpc.ankr.com/eth",
    "https://ethereum.publicnode.com",
]

# Risk ranking used for switch suggestions (lower is safer).
RISK_RANK = {"basso": 0, "medio": 1, "alto": 2}
PROTOCOL_RISK = {
    "Lido": "basso",
    "Aave": "basso",
    "Compound": "basso",
    "Spark/Sky": "basso",
    "Fluid": "medio",
    "Wallet": "basso",
}

STABLE_ASSETS = {"USDC", "USDT", "DAI", "USDS", "sUSDS", "sDAI", "GHO"}
ETH_ASSETS = {"ETH", "WETH", "stETH", "wstETH", "aEthWETH", "aEthwstETH"}

# Typical gas units for a withdraw+deposit style move (estimate only).
SWITCH_GAS_UNITS = 220_000
DUST_SATS = 1000
DEFAULT_GAP_LIMIT = 20
HISTORY_DAYS = 90
