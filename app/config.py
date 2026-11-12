"""Runtime configuration. No personal data, no keys, no seeds."""
from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

APP_NAME = "Monitor DeFi"
APP_VERSION = "0.2.2"
DEFAULT_PORT = int(os.environ.get("PORT", "3847"))

_DEFAULT_DATA = Path(__file__).resolve().parents[1] / "data"
_log = logging.getLogger("app.config")

# Replaced by ensure_data_dir(). Import must not raise if the mount is read-only.
DATA_DIR = Path(os.environ.get("DATA_DIR", str(_DEFAULT_DATA)))
DB_PATH = DATA_DIR / "monitor.db"
DATA_PERSISTENT = True
_READY = False


def requested_data_dir() -> Path:
    return Path(os.environ.get("DATA_DIR", str(_DEFAULT_DATA)))


def directory_is_writable(path: Path) -> bool:
    """True when path exists as a directory and this process can create a file in it."""
    try:
        path.mkdir(parents=True, exist_ok=True)
        if not path.is_dir():
            return False
        probe = path / ".write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def select_writable_dir(requested: Path) -> tuple[Path, str | None]:
    """Pick requested when it is writable, otherwise a fresh temp dir.

    Returns (directory, problem). problem is None when requested itself is used.
    Never raises: a read-only mount must not take the process down.
    """
    if directory_is_writable(requested):
        return requested, None
    try:
        fallback = Path(tempfile.mkdtemp(prefix="nodo-defi-monitor-"))
    except OSError as exc:
        return requested, f"{requested} is not writable and no temporary directory could be created ({exc})"
    return fallback, (
        f"{requested} is not writable (Docker often creates a missing bind-mount source as root). "
        f"Using temporary directory {fallback}. The dashboard will open, but wallets and settings "
        "will be lost when the container stops. On Umbrel, nodo-defi-monitor/data must exist and "
        "be owned by uid 1000 before the volume is mounted."
    )


def _assign(directory: Path, problem: str | None) -> Path:
    global DATA_DIR, DB_PATH, DATA_PERSISTENT, _READY
    DATA_DIR = directory
    DB_PATH = directory / "monitor.db"
    DATA_PERSISTENT = problem is None
    _READY = True
    if problem:
        _log.error("DATA_DIR is not usable: %s", problem)
    return DATA_DIR


def ensure_data_dir() -> Path:
    """Resolve DATA_DIR once. Later calls keep the chosen directory."""
    if _READY:
        return DATA_DIR
    directory, problem = select_writable_dir(requested_data_dir())
    return _assign(directory, problem)


def abandon_unwritable_data_dir(reason: BaseException | None = None) -> Path:
    """Leave the current directory after a failed database open and use a temp dir."""
    global _READY
    _READY = False
    requested = requested_data_dir()
    # select_writable_dir would keep a still-writable requested path. Force a temp dir.
    try:
        fallback = Path(tempfile.mkdtemp(prefix="nodo-defi-monitor-"))
    except OSError:
        directory, problem = select_writable_dir(requested)
        return _assign(directory, problem or "database open failed")
    detail = f"{reason}" if reason else "database open failed"
    return _assign(
        fallback,
        f"could not open the database in {DATA_DIR} ({detail}). Using temporary directory {fallback}. "
        "The dashboard will open, but wallets and settings will not persist.",
    )


ensure_data_dir()

DEFAULT_RPC = os.environ.get("ETH_RPC_URL", "https://ethereum.publicnode.com")
DEFAULT_ELECTRS_HOST = os.environ.get("ELECTRS_HOST", "")
DEFAULT_ELECTRS_PORT = int(os.environ.get("ELECTRS_PORT", "50001"))
# Umbrel names the Electrs container electrs_electrs_1 and serves Electrum on 50001.
ELECTRS_CANDIDATES = (
    ("electrs_electrs_1", 50001),
    ("fulcrum_fulcrum_1", 50001),
)
DEFAULT_BTC_PRICE_URL = os.environ.get(
    "BTC_PRICE_URL",
    "https://api.coinbase.com/v2/prices/BTC-USD/spot",
)

# Fallback public Ethereum RPC list (no API keys required).
PUBLIC_RPCS = [
    "https://ethereum.publicnode.com",
    "https://cloudflare-eth.com",
    "https://rpc.mevblocker.io",
]

# Risk ranking used for switch suggestions (lower is safer).
RISK_RANK = {"basso": 0, "medio": 1, "alto": 2}
PROTOCOL_RISK = {
    "Lido": "basso",
    "Rocket Pool": "basso",
    "Aave": "basso",
    "Compound": "basso",
    "Spark": "basso",
    "SparkLend": "basso",
    "Sky": "basso",
    "Spark/Sky": "basso",
    "Fluid": "medio",
    "Fluid BNB": "medio",
    "Maple": "medio",
    "Wallet": "basso",
}

STABLE_ASSETS = {
    "USDC", "USDT", "DAI", "USDS", "sUSDS", "stUSDS", "sDAI", "GHO",
    "spUSDC", "spUSDT", "syrupUSDC", "syrupUSDT", "fUSDC", "fUSDT",
}
ETH_ASSETS = {"ETH", "WETH", "stETH", "wstETH", "rETH", "aEthWETH", "aEthwstETH"}

# Typical gas units for a withdraw+deposit style move (estimate only).
SWITCH_GAS_UNITS = 220_000
DUST_SATS = 1000
DEFAULT_GAP_LIMIT = 20
HISTORY_DAYS = 90
