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
    "aave.com",
    "pro.aave.com",
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
    "Rocket Pool", "SparkLend", "Fluid BNB", "Compound", "Aave V4", "Aave", "Spark", "Fluid", "Lido", "Sky", "Maple",
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
    if name == "Aave V4":
        return _official("https://aave.com/pro")
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


# Destination tier 1 is the safest core. Tier 2 is allowed only when the
# position being left is already tier 2 or riskier. Anything else is never a destination.
_TIER_1 = {
    ("AAVE", "USDC"), ("AAVE", "USDT"), ("AAVE", "WETH"), ("AAVE", "ETH"),
    ("AAVE V4", "USDC"), ("AAVE V4", "USDT"), ("AAVE V4", "WETH"), ("AAVE V4", "ETH"),
    ("COMPOUND", "USDC"), ("COMPOUND", "USDT"), ("COMPOUND", "WETH"), ("COMPOUND", "ETH"),
    ("LIDO", "STETH"), ("LIDO", "WSTETH"),
}
_TIER_2 = {
    ("ROCKET POOL", "RETH"),
}
_EXCLUDED_WORDS = ("risk capital", "risk-capital", "junior", "tranche", "leverag", "loop", "points", "farm")
_MIN_TVL_USD = 50_000_000
_MAX_UTILIZATION = 95.0
_MIN_AGE_DAYS = 183
_STABLE_APY_CAP = 15.0


def _symbol(asset: str | None) -> str:
    return (asset or "").upper().replace(" ", "")


def _product_text(row: dict[str, Any]) -> str:
    return " ".join(
        str(row.get(key) or "")
        for key in ("protocol", "asset", "pool_meta", "name")
    ).lower()


def hard_excluded(row: dict[str, Any]) -> bool:
    """Products that add risk and are never a destination."""
    if "STUSDS" in _symbol(row.get("asset")):
        return True
    text = _product_text(row)
    return any(word in text for word in _EXCLUDED_WORDS)


def destination_tier(protocol: str, asset: str | None) -> int | None:
    if hard_excluded({"protocol": protocol, "asset": asset}):
        return None
    key = ((protocol or "").upper(), _symbol(asset))
    if key in _TIER_1:
        return 1
    if key in _TIER_2:
        return 2
    return None


def source_tier(protocol: str, asset: str | None) -> int:
    """A position we do not list is riskier than tier 2, so it may move down."""
    tier = destination_tier(protocol, asset)
    return tier if tier is not None else 3


def utilization_percent(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        util = float(value)
    except (TypeError, ValueError):
        return None
    if util <= 1:
        util *= 100
    return util


def _block_reason(dest: dict[str, Any], position_usd: float) -> str | None:
    if hard_excluded(dest) or destination_tier(dest.get("protocol"), dest.get("asset")) is None:
        return "non consentito"
    tvl = dest.get("tvl_usd")
    if tvl is not None and float(tvl) < _MIN_TVL_USD:
        return "tvl"
    util = utilization_percent(dest.get("utilization"))
    if util is not None and util > _MAX_UTILIZATION:
        return "utilizzo"
    available = dest.get("available_usd")
    if available is None and tvl is not None and util is not None:
        available = float(tvl) * (1 - util / 100)
    if available is not None and float(available) < float(position_usd):
        return "liquidità"
    age = dest.get("age_days")
    if age is not None and float(age) < _MIN_AGE_DAYS:
        return "nuovo"
    return None


def _anomaly_reason(dest: dict[str, Any], live: float, apy30: Any) -> str | None:
    """A tempting rate that must not be offered as a switch."""
    try:
        base = float(apy30) if apy30 is not None else 0.0
    except (TypeError, ValueError):
        base = 0.0
    spiked = base > 0 and live > 2 * base
    rich_stable = bucket(str(dest.get("asset") or "")) == BUCKET_STABLE and live > _STABLE_APY_CAP
    if not spiked and not rich_stable:
        return None
    if hard_excluded(dest):
        return "Questo mercato aggiunge rischio e il tasso è fuori scala. Non è un consiglio."
    if spiked:
        return "Il tasso è più del doppio della sua media a 30 giorni."
    return "Il tasso di una stablecoin è sopra il 15%."


def _risk_label(src_tier: int, dest_tier: int) -> str:
    if dest_tier < src_tier:
        return "Rischio: più basso"
    return "Rischio: uguale"


def classify_switches(positions: list[dict[str, Any]], gas_usd: float) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Safe switches, plus anomalous rates that must stay out of the suggestion cards."""
    ideas: list[dict[str, Any]] = []
    anomalies: list[dict[str, Any]] = []
    seen_anomaly: set[tuple[str, str, str]] = set()
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
        seen_extra: set[tuple[Any, Any]] = set()
        for row in rows:
            for alt in row.get("alternatives") or []:
                key = (alt.get("protocol"), alt.get("asset"))
                if key in held or key in seen_extra:
                    continue
                seen_extra.add(key)
                extras.append(alt)
        destinations = list(rows) + extras
        for src in rows:
            src_bucket = bucket(src["asset"])
            src_tier = source_tier(src["protocol"], src.get("asset"))
            src_live = float(src["apy"])
            src_apy, _src_spike = decision_apy(src_live, src.get("apy30"))
            for dest in destinations:
                if dest is src:
                    continue
                if dest.get("owner") not in {None, owner}:
                    continue
                if bucket(dest.get("asset") or "") != src_bucket:
                    continue
                if dest.get("apy") is None:
                    continue
                if dest.get("protocol") == src["protocol"] and dest.get("asset") == src["asset"]:
                    continue
                usd = float(src.get("usd") or 0)
                if usd < 50:
                    continue
                dest_live = float(dest["apy"])
                if dest_live <= src_live:
                    continue
                wallet_label = _wallet_label(src)
                reason = _anomaly_reason(dest, dest_live, dest.get("apy30"))
                if reason:
                    marker = (owner, _wallet_identity(src), str(dest.get("protocol")), str(dest.get("asset")))
                    if marker not in seen_anomaly:
                        seen_anomaly.add(marker)
                        anomalies.append({
                            "owner": owner,
                            "wallet_label": wallet_label,
                            "usd": usd,
                            "protocol": dest.get("protocol"),
                            "asset": dest.get("asset"),
                            "apy": dest_live,
                            "apy30": dest.get("apy30"),
                            "reason": reason,
                        })
                    continue
                if _block_reason(dest, usd):
                    continue
                dest_tier = destination_tier(dest.get("protocol"), dest.get("asset"))
                if dest_tier is None or dest_tier > src_tier:
                    continue
                gross = usd * (dest_live - src_apy) / 100.0
                net = gross - gas_usd
                if net <= 0:
                    continue
                ideas.append(
                    {
                        "owner": owner,
                        "wallet_label": wallet_label,
                        "wallet_key": _wallet_identity(src),
                        "from": f"{src['protocol']} {src['asset']}",
                        "to": f"{dest['protocol']} {dest['asset']}",
                        "usd": usd,
                        "from_apy": src_live,
                        "to_apy": dest_live,
                        "from_apy30": src.get("apy30"),
                        "to_apy30": dest.get("apy30"),
                        "from_rate": src_apy,
                        "to_rate": dest_live,
                        "unstable": False,
                        "apy_source": "DefiLlama",
                        "apy_at": int(time.time()),
                        "from_tier": src_tier,
                        "to_tier": dest_tier,
                        "risk_label": _risk_label(src_tier, dest_tier),
                        "from_risk": _risk_label(src_tier, dest_tier),
                        "to_risk": _risk_label(src_tier, dest_tier),
                        "gas_usd": gas_usd,
                        "gross_year_usd": round(gross, 2),
                        "net_year_usd": round(net, 2),
                        "to_protocol": dest["protocol"],
                        "to_asset": dest["asset"],
                        "lead": f"{_owner_label(owner)} · Wallet: {wallet_label}: {src['protocol']} {src['asset']} verso {dest['protocol']} {dest['asset']}",
                        "url": official_yield_url(dest["protocol"], dest["asset"]),
                    }
                )
    best: dict[str, dict[str, Any]] = {}
    for idea in ideas:
        key = idea["owner"] + "|" + str(idea.get("wallet_key") or idea.get("wallet_label") or "") + "|" + idea["from"]
        prev = best.get(key)
        if not prev or idea["net_year_usd"] > prev["net_year_usd"]:
            best[key] = idea
    out = sorted(best.values(), key=lambda i: i["net_year_usd"], reverse=True)
    for idea in out:
        idea.pop("wallet_key", None)
    return out, anomalies


def suggest_switches(positions: list[dict[str, Any]], gas_usd: float) -> list[dict[str, Any]]:
    """Only an allowlisted destination of equal or lower tier, with a gain after gas."""
    ideas, _anomalies = classify_switches(positions, gas_usd)
    return ideas


def _wallet_label(src: dict[str, Any]) -> str:
    return str(src.get("label") or "").strip() or "Portafoglio"


def _wallet_identity(src: dict[str, Any]) -> str:
    return str(src.get("address") or src.get("label") or "").strip().lower()


def _owner_label(owner: str) -> str:
    return {"io": "Io", "moglie": "Moglie"}.get(owner, owner)


# Same project and symbol the positions table uses, so a suggestion cannot
# quote a different DefiLlama pool than the row it came from.
_SWITCH_MARKETS = (
    ("aave-v3", "Aave", "USDC", "USDC", ""),
    ("aave-v3", "Aave", "USDT", "USDT", ""),
    ("aave-v3", "Aave", "DAI", "DAI", ""),
    ("aave-v3", "Aave", "WETH", "WETH", ""),
    ("aave-v4", "Aave V4", "USDC", "USDC", "Core"),
    ("aave-v4", "Aave V4", "USDT", "USDT", "Core"),
    ("aave-v4", "Aave V4", "WETH", "WETH", "Core"),
    ("compound-v3", "Compound", "USDC", "USDC", ""),
    ("compound-v3", "Compound", "USDT", "USDT", ""),
    ("compound-v3", "Compound", "WETH", "ETH", ""),
    ("lido", "Lido", "stETH", "STETH", ""),
    ("rocket-pool", "Rocket Pool", "rETH", "RETH", ""),
    ("fluid-lending", "Fluid", "USDC", "USDC", ""),
    ("fluid-lending", "Fluid", "USDT", "USDT", ""),
    ("maple", "Maple", "USDC", "USDC", ""),
    ("maple", "Maple", "USDT", "USDT", ""),
)


async def market_alternatives(positions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Same-bucket yields from the same pool matcher the positions table uses."""
    from .markets import apy_for

    held_buckets = {bucket(p["asset"]) for p in positions if p.get("protocol") != "Wallet"}
    if not held_buckets:
        return []
    alts: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for project, protocol, asset, symbol, pool_meta in _SWITCH_MARKETS:
        if bucket(asset) not in held_buckets:
            continue
        key = (protocol, asset, pool_meta)
        if key in seen:
            continue
        apy, apy30, pool = await apy_for(project, symbol, "ethereum", pool_meta or None)
        if apy is None:
            continue
        seen.add(key)
        pool = pool or {}
        tvl = pool.get("tvlUsd")
        util = utilization_percent(pool.get("utilization"))
        available = None
        if tvl is not None and util is not None:
            available = float(tvl) * (1 - util / 100)
        alts.append(
            {
                "protocol": protocol,
                "asset": asset,
                "apy": float(apy),
                "apy30": float(apy30) if apy30 is not None else float(apy),
                "usd": 0,
                "owner": None,
                "market_only": True,
                "pool_id": pool.get("pool"),
                "pool_meta": pool.get("poolMeta") or "",
                "tvl_usd": None if tvl is None else float(tvl),
                "utilization": util,
                "available_usd": available,
                "age_days": _pool_age_days(pool),
                "apy_source": "DefiLlama",
            }
        )
    return alts


def _pool_age_days(pool: dict[str, Any]) -> float | None:
    raw = pool.get("createdAt") or pool.get("listedAt") or pool.get("inception")
    if raw in (None, ""):
        return None
    try:
        stamp = float(raw)
    except (TypeError, ValueError):
        return None
    if stamp > 10_000_000_000:
        stamp /= 1000
    if stamp <= 0:
        return None
    return (time.time() - stamp) / 86400


async def gas_usd_now(override: float | None, eth_price: float) -> float:
    try:
        wei = await evm.gas_price_wei()
    except Exception:
        wei = 0
    return estimate_gas_usd(wei, eth_price, override)
