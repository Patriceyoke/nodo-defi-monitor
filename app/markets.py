"""DefiLlama yields/prices and a conservative protocol health snapshot."""
from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from .protocols import DAI, STETH, USDC, USDS, USDT, WETH

log = logging.getLogger("monitor.markets")

LLAMA_POOLS = "https://yields.llama.fi/pools"
LLAMA_CHART = "https://yields.llama.fi/chart/{pool}"
LLAMA_PRICES = "https://coins.llama.fi/prices/current/{coins}"
COINBASE_BTC = "https://api.coinbase.com/v2/prices/BTC-USD/spot"
COINBASE_FX = "https://api.coinbase.com/v2/exchange-rates?currency=USD"
LLAMA_TVL = "https://api.llama.fi/protocol/{slug}"

# Cache live market data in process memory to avoid hammering public APIs.
_CACHE: dict[str, tuple[float, Any]] = {}
POOLS_TTL = 6 * 60 * 60
PRICE_TTL = 15 * 60


def invalidate() -> None:
    """Drop price, APY and FX caches so the next refresh reads the network."""
    _CACHE.clear()


def fetched_at(url: str) -> float | None:
    hit = _CACHE.get(url)
    return hit[0] if hit else None


async def _get_json(url: str, ttl: float) -> Any:
    now = time.time()
    hit = _CACHE.get(url)
    if hit and now - hit[0] < ttl:
        return hit[1]
    async with httpx.AsyncClient(timeout=12.0) as client:
        resp = await client.get(url, headers={"User-Agent": "monitor-defi/0.1 (read-only homelab)"})
        resp.raise_for_status()
        data = resp.json()
    _CACHE[url] = (now, data)
    return data


def _norm_symbol(symbol: str) -> str:
    return (symbol or "").upper().replace(" ", "")


# Llama's chain label is not always the same word we use internally.
LLAMA_CHAIN_NAMES = {
    "ethereum": {"ethereum", "eth"},
    "bsc": {"bsc", "binance", "binance smart chain"},
}


def _chain_ok(pool_chain: str, chain: str) -> bool:
    names = LLAMA_CHAIN_NAMES.get(chain, {chain.lower()})
    return (pool_chain or "").lower() in names


async def llama_pools() -> list[dict[str, Any]]:
    data = await _get_json(LLAMA_POOLS, POOLS_TTL)
    return list(data.get("data") or [])


async def match_pool(project: str | None, symbol: str | None, chain: str = "ethereum") -> dict[str, Any] | None:
    """Exact project + symbol on the requested chain.

    When several pools share that symbol, prefer the one without poolMeta
    (the main market, not Umbrella or a collateral row) and then the largest TVL.
    A substring match is intentionally not used: "ETH" must not select "WETH".
    """
    if not project or not symbol:
        return None
    pools = await llama_pools()
    want = _norm_symbol(symbol)
    candidates = [
        p for p in pools
        if _chain_ok(p.get("chain") or "", chain)
        and (p.get("project") or "").lower() == project.lower()
        and _norm_symbol(p.get("symbol") or "") == want
    ]
    if not candidates:
        return None
    main = [p for p in candidates if not (p.get("poolMeta") or "")]
    group = main or candidates
    group.sort(key=lambda p: float(p.get("tvlUsd") or 0), reverse=True)
    return group[0]


async def apy_for(
    project: str | None,
    symbol: str | None,
    chain: str = "ethereum",
) -> tuple[float | None, float | None, dict[str, Any] | None]:
    pool = await match_pool(project, symbol, chain)
    if not pool:
        return None, None, None
    apy = pool.get("apy")
    apy30 = pool.get("apyMean30d") or pool.get("apyPct30D") or pool.get("apyBase")
    try:
        apy_f = float(apy) if apy is not None else None
    except (TypeError, ValueError):
        apy_f = None
    try:
        apy30_f = float(apy30) if apy30 is not None else apy_f
    except (TypeError, ValueError):
        apy30_f = apy_f
    return apy_f, apy30_f, pool


def _llama_coin(token: str) -> tuple[str, str]:
    """Return (llama coin id, lookup key).

    A bare 0x address is Ethereum. "bsc:0x..." selects another chain.
    "ETH" is the native ether price.
    """
    text = (token or "").strip()
    if text.upper() == "ETH":
        return "coingecko:ethereum", "ETH"
    if ":" in text:
        chain, addr = text.split(":", 1)
        chain = chain.lower()
        key = f"{chain}:{addr.lower()}"
        return key, key
    key = "ethereum:" + text.lower()
    return key, key


async def prices_usd(tokens: list[str]) -> dict[str, float]:
    coins: list[str] = []
    wanted: dict[str, str] = {}
    for token in tokens:
        if not token:
            continue
        coin, lookup = _llama_coin(token)
        coins.append(coin)
        wanted[coin] = lookup
    if not coins:
        return {}
    # Llama rejects duplicate ids in one request.
    coins = list(dict.fromkeys(coins))
    url = LLAMA_PRICES.format(coins=",".join(coins))
    data = await _get_json(url, PRICE_TTL)
    out: dict[str, float] = {}
    for key, info in (data.get("coins") or {}).items():
        price = info.get("price")
        if price is None:
            continue
        lookup = wanted.get(key, key)
        out[lookup] = float(price)
        out[key] = float(price)
    _CACHE["source:prices"] = (_CACHE.get(url, (time.time(), None))[0], True)
    return out


async def btc_price_usd() -> float | None:
    try:
        data = await _get_json(COINBASE_BTC, PRICE_TTL)
        amount = data.get("data", {}).get("amount")
        _CACHE["source:btc"] = (_CACHE.get(COINBASE_BTC, (time.time(), None))[0], True)
        return float(amount) if amount is not None else None
    except Exception as exc:  # noqa: BLE001
        log.warning("BTC price failed: %s", exc)
        return None


async def fx_thb() -> float | None:
    """Baht per 1 USD. Coinbase is cached for an hour; the last good rate is kept in SQLite."""
    stored = None
    try:
        from . import db
        raw = db.get_setting("fx_thb")
        if raw:
            stored = float(raw)
    except Exception:
        stored = None
    try:
        data = await _get_json(COINBASE_FX, 6 * 60 * 60)
        thb = (data.get("data") or {}).get("rates", {}).get("THB")
        if thb:
            rate = float(thb)
            if rate > 0 and (stored is None or abs(rate - stored) > 0.0001):
                try:
                    from . import db
                    db.set_setting("fx_thb", f"{rate}")
                except Exception:
                    log.warning("Could not store the THB rate")
            _CACHE["source:fx"] = (_CACHE.get(COINBASE_FX, (time.time(), None))[0], True)
            return rate
    except Exception as exc:  # noqa: BLE001
        log.warning("FX THB failed: %s", exc)
    return stored


async def protocol_health() -> list[dict[str, Any]]:
    """Lightweight checks: peg and utilization from public pool data, only."""
    pools = await llama_pools()
    price_tokens = [STETH, USDC, USDT, DAI, USDS, WETH]
    px = await prices_usd(price_tokens)
    eth = px.get(f"ethereum:{WETH.lower()}") or px.get("ETH") or 0
    steth = px.get(f"ethereum:{STETH.lower()}") or 0
    checks: list[dict[str, Any]] = []

    def peg(name: str, price: float, target: float, warn_bps: float = 0.01) -> dict[str, Any]:
        if not price or not target:
            return {
                "protocol": name,
                "title": f"Peg {name}",
                "detail": "Prezzo non disponibile in questo momento.",
                "tone": "warning",
                "label": "Dato mancante",
            }
        dev = abs(price / target - 1)
        if dev >= 0.02:
            tone, label = "danger", "Peg allontanato"
        elif dev >= warn_bps:
            tone, label = "warning", "Da osservare"
        else:
            tone, label = "success", "Nella norma"
        return {
            "protocol": name,
            "title": f"Peg {name}",
            "detail": f"Prezzo osservato {price:.4f} $ rispetto a {target:.4f} $ (scostamento {dev*100:.2f}%).",
            "tone": tone,
            "label": label,
        }

    checks.append(peg("Lido", steth, eth, 0.008))
    for stable, addr in (("USDC", USDC), ("USDT", USDT), ("DAI", DAI), ("USDS", USDS)):
        checks.append(peg(stable, px.get(f"ethereum:{addr.lower()}") or 0, 1.0, 0.005))

    # Utilization / TVL signal from the largest Ethereum pool of each project.
    wanted = {
        "Aave": ["aave-v3"],
        "Compound": ["compound-v3"],
        "Fluid": ["fluid-lending"],
        "Spark": ["spark-savings"],
        "SparkLend": ["sparklend"],
        "Sky": ["sky-lending"],
        "Maple": ["maple"],
        "Rocket Pool": ["rocket-pool"],
        "Lido": ["lido"],
    }
    for protocol, projects in wanted.items():
        group = [
            p for p in pools
            if (p.get("project") or "").lower() in {x.lower() for x in projects}
            and (p.get("chain") or "").lower() in {"ethereum", "eth"}
        ]
        if not group:
            checks.append(
                {
                    "protocol": protocol,
                    "title": protocol,
                    "detail": "Nessun dato di mercato trovato su DefiLlama in questo momento.",
                    "tone": "warning",
                    "label": "Dato mancante",
                }
            )
            continue
        top = max(group, key=lambda p: float(p.get("tvlUsd") or 0))
        util = top.get("utilization") or top.get("poolMeta")
        apy = top.get("apy")
        tvl = float(top.get("tvlUsd") or 0)
        util_val = None
        if isinstance(util, (int, float)):
            util_val = float(util)
            if util_val <= 1.5:
                # Some APIs return 0-1, others 0-100
                if util_val <= 1:
                    util_val *= 100
        tone, label, detail = "success", "Nella norma", (
            f"Mercato {top.get('symbol')} su Ethereum, TVL circa {tvl:,.0f} $."
        )
        if apy is not None:
            detail += f" APY di riferimento {float(apy):.2f}%."
        if util_val is not None:
            detail += f" Utilizzo osservato {util_val:.0f}%."
            if util_val >= 92:
                tone, label = "danger", "Utilizzo molto alto"
            elif util_val >= 85:
                tone, label = "warning", "Da osservare"
        checks.append(
            {
                "protocol": protocol,
                "title": protocol,
                "detail": detail + " Non è un incidente: è un dato di mercato da tenere d'occhio.",
                "tone": tone,
                "label": label,
            }
        )
    return checks
