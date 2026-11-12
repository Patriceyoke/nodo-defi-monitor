"""Suggest a protocol switch only if yield rises at equal or lower risk, after gas."""
from __future__ import annotations

import time
from typing import Any

from urllib.parse import urlparse

from .config import ETH_ASSETS, PROTOCOL_RISK, RISK_RANK, STABLE_ASSETS, SWITCH_GAS_UNITS
from . import evm
from .protocols import DAI, USDC, USDT, USDS, WETH

# Only these hosts may appear on a suggestion. No aggregators and no pasted URLs.
OFFICIAL_HOSTS = frozenset({
    "app.aave.com",
    "app.compound.xyz",
    "app.spark.finance",
    "fluid.io",
    "stake.lido.fi",
    "app.sky.money",
    "stake.rocketpool.net",
    "maple.finance",
})

_AAVE_UNDERLYING = {"USDC": USDC, "USDT": USDT, "DAI": DAI, "WETH": WETH, "ETH": WETH}
_COMPOUND_MARKET = {"USDC": "usdc-mainnet", "USDT": "usdt-mainnet", "WETH": "weth-mainnet", "ETH": "weth-mainnet"}
_SPARK_SAVINGS = {"USDC": "spusdc", "USDT": "spusdt"}
_KNOWN_PROTOCOLS = (
    "Rocket Pool", "SparkLend", "Fluid BNB", "Compound", "Aave", "Spark", "Fluid", "Lido", "Sky", "Maple",
)

BUCKET_STABLE = "stable"
BUCKET_ETH = "eth"
BUCKET_OTHER = "other"


def _official(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or (parsed.hostname or "") not in OFFICIAL_HOSTS:
        return ""
    return url


def split_destination(label: str) -> tuple[str, str]:
    text = (label or "").strip()
    for name in _KNOWN_PROTOCOLS:
        prefix = name + " "
        if text.startswith(prefix):
            return name, text[len(prefix):].strip()
    return "", text


def official_yield_url(protocol: str, asset: str) -> str:
    """Deep link to the official app for this market, or that app's homepage."""
    name = (protocol or "").strip()
    symbol = (asset or "").strip().upper()
    if name == "Aave":
        underlying = _AAVE_UNDERLYING.get(symbol)
        if underlying:
            return _official(
                "https://app.aave.com/reserve-overview/"
                f"?underlyingAsset={underlying}&marketName=proto_mainnet_v3"
            )
        return _official("https://app.aave.com/")
    if name == "Compound":
        market = _COMPOUND_MARKET.get(symbol)
        if market:
            return _official(f"https://app.compound.xyz/markets/{market}")
        return _official("https://app.compound.xyz/")
    if name == "Spark":
        vault = _SPARK_SAVINGS.get(symbol)
        if vault:
            return _official(f"https://app.spark.finance/savings/{vault}")
        return _official("https://app.spark.finance/savings")
    if name == "SparkLend":
        underlying = _AAVE_UNDERLYING.get(symbol)
        if underlying:
            return _official(
                "https://app.spark.finance/reserve-overview/"
                f"?underlyingAsset={underlying}&marketName=proto_spark_v3"
            )
        return _official("https://app.spark.finance/markets")
    if name == "Fluid":
        return _official("https://fluid.io/1/lending")
    if name == "Fluid BNB":
        return _official("https://fluid.io/")
    if name == "Lido":
        if symbol == "WSTETH":
            return _official("https://stake.lido.fi/wrap")
        return _official("https://stake.lido.fi/")
    if name == "Sky":
        if symbol == "STUSDS":
            return _official("https://app.sky.money/?widget=stusds")
        if symbol in {"SUSDS", "USDS"}:
            return _official("https://app.sky.money/?widget=savings")
        if symbol == "SKY":
            return _official("https://app.sky.money/stake")
        return _official("https://app.sky.money/")
    if name == "Rocket Pool":
        return _official("https://stake.rocketpool.net/")
    if name == "Maple":
        return _official("https://maple.finance/")
    return ""


def with_official_url(idea: dict[str, Any]) -> dict[str, Any]:
    row = dict(idea)
    protocol = str(row.get("to_protocol") or "")
    asset = str(row.get("to_asset") or "")
    if not protocol:
        protocol, asset = split_destination(str(row.get("to") or ""))
    row["url"] = official_yield_url(protocol, asset)
    return row


def bucket(asset: str) -> str:
    symbol = (asset or "").upper()
    if symbol in STABLE_ASSETS or symbol.endswith("USDC") or symbol.endswith("USDT") or symbol.endswith("DAI") or symbol.endswith("USDS"):
        return BUCKET_STABLE
    if symbol in ETH_ASSETS or "ETH" in symbol or "STETH" in symbol:
        return BUCKET_ETH
    return BUCKET_OTHER


def risk_of(protocol: str, asset: str | None = None) -> str:
    symbol = (asset or "").upper()
    # stUSDS funds SKY-backed borrowing. It is not the same risk as Aave or sUSDS.
    if symbol == "STUSDS":
        return "medio"
    return PROTOCOL_RISK.get(protocol, "medio")


def decision_apy(apy: float, apy30: float | None) -> tuple[float, bool]:
    """Use the 30-day average when the live rate is more than twice that average."""
    live = float(apy)
    if apy30 is None:
        return live, False
    try:
        base = float(apy30)
    except (TypeError, ValueError):
        return live, False
    if base > 0 and live > 2 * base:
        return base, True
    return live, False


def estimate_gas_usd(gas_price_wei: int, eth_price: float, override: float | None = None) -> float:
    if override is not None and override > 0:
        return float(override)
    if gas_price_wei <= 0 or eth_price <= 0:
        return 10.0
    return (gas_price_wei * SWITCH_GAS_UNITS / 1e18) * eth_price


MATERIAL_SPREAD_PP = 1.0
HIDE_SECONDS = 7 * 86400


def switch_spread(idea: dict[str, Any]) -> float:
    return float(idea["to_apy"]) - float(idea["from_apy"])


def switch_is_hidden(
    idea: dict[str, Any],
    saved_spread: float | None,
    hidden_at: int | None = None,
    now: int | None = None,
) -> bool:
    """Stay hidden until the gap grows by 1 point, or 7 days pass while it is still proposed.

    A different target is a different suggestion, so it is not passed here.
    """
    if saved_spread is None:
        return False
    if hidden_at is not None:
        moment = int(time.time() if now is None else now)
        if moment - int(hidden_at) >= HIDE_SECONDS:
            return False
    return switch_spread(idea) < float(saved_spread) + MATERIAL_SPREAD_PP - 1e-9


def mark_switch_hides(
    ideas: list[dict[str, Any]],
    hides: list[dict[str, Any]],
    now: int | None = None,
) -> tuple[list[dict[str, Any]], list[tuple[str, str, str]]]:
    """Mark ideas and list hides to drop because the suggestion is visible again."""
    moment = int(time.time() if now is None else now)
    saved = {
        (row["owner"], row["from_position"], row["to_target"]): (float(row["spread"]), int(row.get("created_at") or moment))
        for row in hides
    }
    marked: list[dict[str, Any]] = []
    release: list[tuple[str, str, str]] = []
    for idea in ideas:
        row = dict(idea)
        row["spread"] = switch_spread(row)
        row["url"] = with_official_url(row)["url"]
        key = (row["owner"], row["from"], row["to"])
        remembered = saved.get(key)
        row["hidden"] = switch_is_hidden(
            row,
            None if remembered is None else remembered[0],
            None if remembered is None else remembered[1],
            moment,
        )
        if remembered is not None and not row["hidden"]:
            release.append(key)
        marked.append(row)
    return marked, release


def suggest_switches(positions: list[dict[str, Any]], gas_usd: float) -> list[dict[str, Any]]:
    """Only same-owner, same-asset-bucket, dest risk <= source, net yearly gain > 0 after gas."""
    ideas: list[dict[str, Any]] = []
    # Candidate destinations already held, plus other open rates on held buckets
    # We only compare among current positions and optional "open" yield quotes attached as market_options.
    by_owner: dict[str, list[dict[str, Any]]] = {}
    for pos in positions:
        if pos.get("protocol") == "Wallet":
            continue
        if pos.get("apy") is None:
            continue
        by_owner.setdefault(pos["owner"], []).append(pos)

    for owner, rows in by_owner.items():
        held = {(row.get("protocol"), row.get("asset")) for row in rows}
        extras = []
        for row in rows:
            for alt in row.get("alternatives") or []:
                if (alt.get("protocol"), alt.get("asset")) in held:
                    continue
                extras.append(alt)
        destinations = list(rows) + extras
        for src in rows:
            src_bucket = bucket(src["asset"])
            src_risk = risk_of(src["protocol"], src.get("asset"))
            src_live = float(src["apy"])
            src_apy, src_spike = decision_apy(src_live, src.get("apy30"))
            for dest in destinations:
                if dest is src:
                    continue
                if dest.get("owner") not in {None, owner}:
                    continue
                if bucket(dest["asset"]) != src_bucket:
                    continue
                if dest.get("apy") is None:
                    continue
                dest_risk = risk_of(dest["protocol"], dest.get("asset"))
                if RISK_RANK[dest_risk] > RISK_RANK[src_risk]:
                    continue
                dest_live = float(dest["apy"])
                dest_apy, dest_spike = decision_apy(dest_live, dest.get("apy30"))
                if dest_apy <= src_apy:
                    continue
                # Same protocol+asset is not a switch
                if dest.get("protocol") == src["protocol"] and dest.get("asset") == src["asset"]:
                    continue
                usd = float(src.get("usd") or 0)
                if usd < 50:
                    continue
                gross = usd * (dest_apy - src_apy) / 100.0
                net = gross - gas_usd
                if net <= 0:
                    continue
                ideas.append(
                    {
                        "owner": owner,
                        "from": f"{src['protocol']} {src['asset']}",
                        "to": f"{dest['protocol']} {dest['asset']}",
                        "usd": usd,
                        "from_apy": src_live,
                        "to_apy": dest_live,
                        "from_apy30": src.get("apy30"),
                        "to_apy30": dest.get("apy30"),
                        "from_rate": src_apy,
                        "to_rate": dest_apy,
                        "unstable": bool(src_spike or dest_spike),
                        "apy_source": "DefiLlama",
                        "apy_at": int(time.time()),
                        "from_risk": src_risk,
                        "to_risk": dest_risk,
                        "gas_usd": gas_usd,
                        "gross_year_usd": round(gross, 2),
                        "net_year_usd": round(net, 2),
                        "to_protocol": dest["protocol"],
                        "to_asset": dest["asset"],
                        "lead": f"{_owner_label(owner)}: {src['protocol']} {src['asset']} verso {dest['protocol']} {dest['asset']}",
                        "url": official_yield_url(dest["protocol"], dest["asset"]),
                    }
                )
    # Keep the best idea per source position
    best: dict[str, dict[str, Any]] = {}
    for idea in ideas:
        key = idea["owner"] + "|" + idea["from"]
        prev = best.get(key)
        if not prev or idea["net_year_usd"] > prev["net_year_usd"]:
            best[key] = idea
    out = sorted(best.values(), key=lambda i: i["net_year_usd"], reverse=True)
    return out


def _owner_label(owner: str) -> str:
    return {"io": "Io", "moglie": "Moglie"}.get(owner, owner)


# Same project and symbol the positions table uses, so a suggestion cannot
# quote a different DefiLlama pool than the row it came from.
_SWITCH_MARKETS = (
    ("aave-v3", "Aave", "USDC", "USDC"),
    ("aave-v3", "Aave", "USDT", "USDT"),
    ("aave-v3", "Aave", "DAI", "DAI"),
    ("aave-v3", "Aave", "WETH", "WETH"),
    ("compound-v3", "Compound", "USDC", "USDC"),
    ("compound-v3", "Compound", "USDT", "USDT"),
    ("compound-v3", "Compound", "WETH", "ETH"),
    ("spark-savings", "Spark", "USDC", "USDC"),
    ("spark-savings", "Spark", "USDT", "USDT"),
    ("sparklend", "SparkLend", "USDC", "USDC"),
    ("sparklend", "SparkLend", "USDT", "USDT"),
    ("sky-lending", "Sky", "sUSDS", "SUSDS"),
    ("sky-lending", "Sky", "stUSDS", "STUSDS"),
    ("lido", "Lido", "stETH", "STETH"),
    ("rocket-pool", "Rocket Pool", "rETH", "RETH"),
    ("fluid-lending", "Fluid", "USDC", "USDC"),
    ("fluid-lending", "Fluid", "USDT", "USDT"),
    ("maple", "Maple", "USDC", "USDC"),
    ("maple", "Maple", "USDT", "USDT"),
)


async def market_alternatives(positions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Same-bucket yields from the same pool matcher the positions table uses."""
    from .markets import apy_for

    held_buckets = {bucket(p["asset"]) for p in positions if p.get("protocol") != "Wallet"}
    if not held_buckets:
        return []
    alts: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for project, protocol, asset, symbol in _SWITCH_MARKETS:
        if bucket(asset) not in held_buckets:
            continue
        key = (protocol, asset)
        if key in seen:
            continue
        apy, apy30, pool = await apy_for(project, symbol, "ethereum")
        if apy is None:
            continue
        seen.add(key)
        alts.append(
            {
                "protocol": protocol,
                "asset": asset,
                "apy": float(apy),
                "apy30": float(apy30) if apy30 is not None else float(apy),
                "usd": 0,
                "owner": None,
                "market_only": True,
                "pool_id": (pool or {}).get("pool"),
                "apy_source": "DefiLlama",
            }
        )
    return alts


async def gas_usd_now(override: float | None, eth_price: float) -> float:
    try:
        wei = await evm.gas_price_wei()
    except Exception:
        wei = 0
    return estimate_gas_usd(wei, eth_price, override)
