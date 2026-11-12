"""Read-only NFT inventory. No listings, no signatures, no wallet keys.

Keyless path: Alchemy's public demo key for the inventory and OpenSea floor,
then CoinGecko for a USD floor and the recent-sale average. If that demo key
is rate-limited, Blockscout still returns the list and prices stay unavailable.

An optional Alchemy key replaces the demo key. An optional OpenSea key adds
the current listing price when the marketplace exposes one.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any

import httpx

from . import db
from .protocols import CHAINS

log = logging.getLogger("monitor.nft")

# Public example key documented by Alchemy for the NFT API. A personal key,
# stored only in SQLite, replaces it when the user saves one.
ALCHEMY_DEMO_KEY = "demo"
PAGE_SIZE = 100
COINGECKO_CONTRACT_CAP = 25
OPENSEA_LISTING_CAP = 12
CACHE_TTL = 10 * 60
SUGGESTION_PREMIUM = 0.05

# chain -> indexer hosts and the native coin used for floors.
NFT_CHAINS: dict[str, dict[str, str]] = {
    "ethereum": {
        "alchemy": "eth-mainnet",
        "coingecko": "ethereum",
        "blockscout": "https://eth.blockscout.com",
        "opensea": "ethereum",
        "native": "ETH",
        "native_id": "ethereum",
    },
    "bsc": {
        "alchemy": "bnb-mainnet",
        "coingecko": "binance-smart-chain",
        "blockscout": "https://bsc.blockscout.com",
        "opensea": "bsc",
        "native": "BNB",
        "native_id": "binancecoin",
    },
    "polygon": {
        "alchemy": "polygon-mainnet",
        "coingecko": "polygon-pos",
        "blockscout": "https://polygon.blockscout.com",
        "opensea": "matic",
        "native": "POL",
        "native_id": "polygon-ecosystem-token",
    },
    "arbitrum": {
        "alchemy": "arb-mainnet",
        "coingecko": "arbitrum-one",
        "blockscout": "https://arbitrum.blockscout.com",
        "opensea": "arbitrum",
        "native": "ETH",
        "native_id": "ethereum",
    },
    "base": {
        "alchemy": "base-mainnet",
        "coingecko": "base",
        "blockscout": "https://base.blockscout.com",
        "opensea": "base",
        "native": "ETH",
        "native_id": "ethereum",
    },
}

_URL_RE = re.compile(r"(https?://|www\.)", re.IGNORECASE)
_SPAM_WORD_RE = re.compile(r"\b(claim|reward|visit|voucher)\b", re.IGNORECASE)
_CACHE: dict[str, tuple[float, Any]] = {}


def spam_reason(item: dict[str, Any]) -> str | None:
    """Return why an item should be hidden, or None if it can be shown.

    Hard rules: source spam flag, claim/reward/visit/voucher or a URL in the
    name or description, unverified ERC-1155, and unverified tokens that have
    neither a floor nor any recent sales.
    """
    if item.get("source_spam"):
        return "La fonte lo segna come spam."
    text = " ".join(
        str(item.get(key) or "")
        for key in ("name", "description", "collection")
    )
    if _URL_RE.search(text) or _SPAM_WORD_RE.search(text):
        return "Nel nome o nella descrizione c'è un link oppure un invito a claim, reward, visit o voucher."
    token_type = str(item.get("token_type") or "").upper().replace("-", "")
    verified = bool(item.get("verified"))
    if "1155" in token_type and not verified:
        return "ERC-1155 non verificato: spesso è un airdrop non richiesto."
    has_floor = item.get("floor_native") is not None or item.get("floor_usd") is not None
    has_sales = item.get("recent_sale_native") is not None or (item.get("recent_sales_count") or 0) > 0
    if not has_floor and not has_sales and not verified:
        return "Nessun floor e nessuna vendita recente, e la collezione non è verificata."
    return None


def suggest_sale(item: dict[str, Any]) -> dict[str, Any]:
    """Suggest a sale price from the floor, otherwise from recent sales.

    The suggestion is a number to look at, not an order. Listing and signing
    stay outside this app.
    """
    floor_n = _pos(item.get("floor_native"))
    floor_u = _pos(item.get("floor_usd"))
    recent_n = _pos(item.get("recent_sale_native"))
    recent_u = _pos(item.get("recent_sale_usd"))
    listed_n = _pos(item.get("list_price_native"))
    native = item.get("native_symbol") or ""
    out = {
        "suggestion_native": None,
        "suggestion_usd": None,
        "suggestion_reason": "Prezzo non disponibile.",
        "price_status": "unavailable",
    }
    if floor_n is None and floor_u is None and recent_n is None and recent_u is None:
        if listed_n is not None:
            out["suggestion_reason"] = (
                "Risulta in vendita, ma non c'è un floor di collezione con cui confrontarlo. Prezzo non disponibile."
            )
        return out
    if floor_n is not None or floor_u is not None:
        out["suggestion_native"] = None if floor_n is None else round(floor_n * (1 + SUGGESTION_PREMIUM), 6)
        out["suggestion_usd"] = None if floor_u is None else round(floor_u * (1 + SUGGESTION_PREMIUM), 2)
        out["price_status"] = "ok"
        if listed_n is not None and floor_n is not None and listed_n + 1e-12 < floor_n:
            out["suggestion_reason"] = (
                f"È già in vendita sotto il floor. Suggerimento: il floor più un 5%, "
                f"per non svendere{(' in ' + native) if native else ''}."
            )
        elif listed_n is not None:
            out["suggestion_reason"] = (
                "È già in vendita. Il suggerimento resta il floor più un 5%: non è un ordine e da qui non si mette in vendita nulla."
            )
        else:
            out["suggestion_reason"] = (
                "Leggermente sopra il floor della collezione, per non svendere rispetto al mercato. "
                "È solo un numero di riferimento."
            )
        return out
    out["suggestion_native"] = recent_n
    out["suggestion_usd"] = recent_u
    out["price_status"] = "ok"
    out["suggestion_reason"] = (
        "Non c'è un floor affidabile: il suggerimento è la media delle vendite delle ultime 24 ore."
    )
    return out


def _pos(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    return number


def _cache_get(key: str) -> Any | None:
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1]
    return None


def _cache_set(key: str, value: Any) -> None:
    _CACHE[key] = (time.time(), value)


def _image_url(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    if value.startswith("ipfs://"):
        return "https://ipfs.io/ipfs/" + value[len("ipfs://"):]
    if value.startswith("http://") or value.startswith("https://"):
        return value
    return None


def _alchemy_key() -> tuple[str, str]:
    provider = (db.get_setting("nft_api_provider") or "").strip().lower()
    key = (db.get_setting("nft_api_key") or "").strip()
    if provider == "alchemy" and key:
        return key, "alchemy"
    return ALCHEMY_DEMO_KEY, "alchemy-demo"


def _opensea_key() -> str:
    provider = (db.get_setting("nft_api_provider") or "").strip().lower()
    key = (db.get_setting("nft_api_key") or "").strip()
    if provider == "opensea" and key:
        return key
    return ""


def parse_alchemy_nft(raw: dict[str, Any], chain: str) -> dict[str, Any]:
    contract = raw.get("contract") or {}
    meta = contract.get("openSeaMetadata") or {}
    classes = [str(c) for c in (contract.get("spamClassifications") or [])]
    source_spam = bool(contract.get("isSpam")) or any(
        "spam" in c.lower() and "spammy" not in c.lower() for c in classes
    )
    image = raw.get("image") or {}
    image_url = _image_url(image.get("thumbnailUrl") or image.get("cachedUrl") or image.get("originalUrl"))
    verified = str(meta.get("safelistRequestStatus") or "").lower() == "verified"
    native = NFT_CHAINS[chain]["native"]
    return {
        "chain": chain,
        "chain_label": CHAINS.get(chain, {}).get("label", chain),
        "contract": contract.get("address") or "",
        "token_id": str(raw.get("tokenId") or ""),
        "token_type": raw.get("tokenType") or contract.get("tokenType") or "",
        "name": raw.get("name") or meta.get("collectionName") or contract.get("name") or "Senza nome",
        "collection": meta.get("collectionName") or contract.get("name") or "",
        "collection_slug": meta.get("collectionSlug") or "",
        "description": raw.get("description") or meta.get("description") or "",
        "image": image_url,
        "verified": verified,
        "source_spam": source_spam,
        "floor_native": _pos(meta.get("floorPrice")),
        "floor_usd": None,
        "recent_sale_native": None,
        "recent_sale_usd": None,
        "recent_sales_count": None,
        "native_symbol": native,
        "listed": None,
        "list_price_native": None,
        "list_price_usd": None,
    }


def parse_blockscout_nft(raw: dict[str, Any], chain: str) -> dict[str, Any]:
    token = raw.get("token") or {}
    metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
    reputation = str(token.get("reputation") or "").lower()
    image = _image_url(raw.get("image_url") or metadata.get("image"))
    return {
        "chain": chain,
        "chain_label": CHAINS.get(chain, {}).get("label", chain),
        "contract": token.get("address_hash") or "",
        "token_id": str(raw.get("id") or ""),
        "token_type": raw.get("token_type") or token.get("type") or "",
        "name": metadata.get("name") or token.get("name") or "Senza nome",
        "collection": token.get("name") or "",
        "collection_slug": "",
        "description": metadata.get("description") or "",
        "image": image,
        "verified": reputation == "ok",
        "source_spam": reputation in {"spam", "blacklisted"},
        "floor_native": None,
        "floor_usd": None,
        "recent_sale_native": None,
        "recent_sale_usd": None,
        "recent_sales_count": None,
        "native_symbol": NFT_CHAINS[chain]["native"],
        "listed": None,
        "list_price_native": None,
        "list_price_usd": None,
    }


def apply_market(item: dict[str, Any], market: dict[str, Any] | None, native_usd: float | None) -> None:
    if market:
        if item.get("floor_usd") is None and market.get("floor_usd") is not None:
            item["floor_usd"] = market["floor_usd"]
        if item.get("floor_native") is None and market.get("floor_native") is not None:
            item["floor_native"] = market["floor_native"]
        item["recent_sale_native"] = market.get("recent_sale_native")
        item["recent_sale_usd"] = market.get("recent_sale_usd")
        item["recent_sales_count"] = market.get("recent_sales_count")
    floor_n = _pos(item.get("floor_native"))
    if item.get("floor_usd") is None and floor_n is not None and native_usd:
        item["floor_usd"] = round(floor_n * native_usd, 2)
    recent_n = _pos(item.get("recent_sale_native"))
    if item.get("recent_sale_usd") is None and recent_n is not None and native_usd:
        item["recent_sale_usd"] = round(recent_n * native_usd, 2)
    list_n = _pos(item.get("list_price_native"))
    if item.get("list_price_usd") is None and list_n is not None and native_usd:
        item["list_price_usd"] = round(list_n * native_usd, 2)


def finalize(item: dict[str, Any]) -> dict[str, Any]:
    reason = spam_reason(item)
    item["hidden"] = reason is not None
    item["hidden_reason"] = reason or ""
    item.update(suggest_sale(item))
    return item


async def _get_json(client: httpx.AsyncClient, url: str, headers: dict[str, str] | None = None) -> Any:
    resp = await client.get(url, headers=headers)
    resp.raise_for_status()
    return resp.json()


async def _alchemy_page(client: httpx.AsyncClient, chain: str, owner: str, key: str) -> list[dict[str, Any]]:
    network = NFT_CHAINS[chain]["alchemy"]
    url = (
        f"https://{network}.g.alchemy.com/nft/v3/{key}/getNFTsForOwner"
        f"?owner={owner}&withMetadata=true&pageSize={PAGE_SIZE}"
    )
    data = await _get_json(client, url)
    return [parse_alchemy_nft(raw, chain) for raw in (data.get("ownedNfts") or [])]


async def _blockscout_page(client: httpx.AsyncClient, chain: str, owner: str) -> list[dict[str, Any]]:
    base = NFT_CHAINS[chain]["blockscout"]
    url = f"{base}/api/v2/addresses/{owner}/nft"
    data = await _get_json(client, url, {"User-Agent": "monitor-defi/0.1"})
    items = data.get("items") if isinstance(data, dict) else None
    return [parse_blockscout_nft(raw, chain) for raw in (items or [])][:PAGE_SIZE]


async def _wallet_nfts(client: httpx.AsyncClient, chain: str, owner: str, alchemy_key: str) -> tuple[list[dict[str, Any]], str | None]:
    cache_key = f"nfts:{chain}:{owner.lower()}:{alchemy_key[:6]}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached, None
    try:
        rows = await _alchemy_page(client, chain, owner, alchemy_key)
        _cache_set(cache_key, rows)
        return rows, None
    except Exception as exc:  # noqa: BLE001
        log.warning("Alchemy NFT %s %s failed: %s", chain, owner, exc)
    try:
        rows = await _blockscout_page(client, chain, owner)
        _cache_set(cache_key, rows)
        return rows, None
    except Exception as exc:  # noqa: BLE001
        log.warning("Blockscout NFT %s %s failed: %s", chain, owner, exc)
        return [], f"NFT su {CHAINS.get(chain, {}).get('label', chain)} non disponibili: {exc}"


async def _native_prices(client: httpx.AsyncClient) -> dict[str, float]:
    cached = _cache_get("native-prices")
    if isinstance(cached, dict):
        return cached
    ids = ",".join(sorted({spec["native_id"] for spec in NFT_CHAINS.values()}))
    url = f"https://api.coingecko.com/api/v3/simple/price?ids={ids}&vs_currencies=usd"
    try:
        data = await _get_json(client, url, {"User-Agent": "monitor-defi/0.1"})
    except Exception as exc:  # noqa: BLE001
        log.warning("Prezzi nativi NFT non disponibili: %s", exc)
        return {}
    out = {}
    for coin_id, row in (data or {}).items():
        usd = _pos((row or {}).get("usd"))
        if usd:
            out[coin_id] = usd
    _cache_set("native-prices", out)
    return out


async def _coingecko_market(client: httpx.AsyncClient, chain: str, contract: str) -> dict[str, Any] | None:
    key = f"cg:{chain}:{contract.lower()}"
    cached = _cache_get(key)
    if cached is not None:
        return cached or None
    platform = NFT_CHAINS[chain]["coingecko"]
    url = f"https://api.coingecko.com/api/v3/nfts/{platform}/contract/{contract}"
    try:
        data = await _get_json(client, url, {"User-Agent": "monitor-defi/0.1", "accept": "application/json"})
    except Exception as exc:  # noqa: BLE001
        log.info("CoinGecko senza mercato per %s %s: %s", chain, contract, exc)
        _cache_set(key, {})
        return None
    floor = data.get("floor_price") or {}
    market = {
        "floor_native": _pos(floor.get("native_currency") if isinstance(floor, dict) else None),
        "floor_usd": _pos(floor.get("usd") if isinstance(floor, dict) else None),
        "recent_sale_native": _pos(data.get("one_day_average_sale_price")),
        "recent_sale_usd": None,
        "recent_sales_count": data.get("one_day_sales"),
    }
    _cache_set(key, market)
    return market


def _parse_listing_price(payload: Any) -> float | None:
    if not isinstance(payload, dict):
        return None
    price = payload.get("price") or {}
    current = price.get("current") if isinstance(price, dict) else None
    if isinstance(current, dict):
        raw = current.get("value")
        decimals = current.get("decimals")
        try:
            if raw is not None and decimals is not None:
                return float(raw) / (10 ** int(decimals))
            if current.get("decimal") is not None or current.get("amount") is not None:
                return float(current.get("decimal") or current.get("amount"))
        except (TypeError, ValueError):
            return None
    return None


async def _listing(client: httpx.AsyncClient, item: dict[str, Any], api_key: str) -> None:
    slug = item.get("collection_slug") or ""
    token_id = item.get("token_id") or ""
    chain = NFT_CHAINS.get(item["chain"], {}).get("opensea")
    if not slug or not token_id or not chain:
        return
    url = f"https://api.opensea.io/api/v2/listings/collection/{slug}/nfts/{token_id}/best"
    try:
        data = await _get_json(client, url, {"x-api-key": api_key, "accept": "application/json"})
    except Exception as exc:  # noqa: BLE001
        log.info("Quotazione OpenSea non disponibile per %s: %s", slug, exc)
        return
    price = _parse_listing_price(data)
    if price is None and isinstance(data, dict):
        price = _parse_listing_price(data.get("listing") or data.get("order") or {})
    if price is None:
        item["listed"] = False
        return
    item["listed"] = True
    item["list_price_native"] = price


async def collect_nfts(owner: str) -> dict[str, Any]:
    """NFTs for the saved Ethereum-style wallets of one owner filter."""
    from .validation import normalize_owner

    owner = normalize_owner(owner, allow_all=True)
    wallets = db.list_evm(None if owner == "tutti" else owner)
    alchemy_key, source = _alchemy_key()
    opensea = _opensea_key()
    errors: list[str] = []
    items: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=25.0) as client:
        native = await _native_prices(client)
        for wallet in wallets:
            address = wallet["address"]
            for chain in NFT_CHAINS:
                rows, err = await _wallet_nfts(client, chain, address, alchemy_key)
                if err:
                    errors.append(f"{wallet['label']}: {err}")
                for row in rows:
                    row["owner"] = wallet["owner"]
                    row["label"] = wallet["label"]
                    row["wallet"] = address
                    items.append(row)
        markets: dict[tuple[str, str], dict[str, Any] | None] = {}
        contracts = []
        for row in items:
            key = (row["chain"], (row.get("contract") or "").lower())
            if key[1] and key not in markets:
                contracts.append(key)
        for key in contracts[:COINGECKO_CONTRACT_CAP]:
            markets[key] = await _coingecko_market(client, key[0], key[1])
        if len(contracts) > COINGECKO_CONTRACT_CAP:
            errors.append(
                f"Floor CoinGecko calcolato solo per {COINGECKO_CONTRACT_CAP} collezioni, per non superare il limite gratuito."
            )
        listed_budget = OPENSEA_LISTING_CAP if opensea else 0
        for row in items:
            key = (row["chain"], (row.get("contract") or "").lower())
            native_usd = native.get(NFT_CHAINS[row["chain"]]["native_id"])
            apply_market(row, markets.get(key), native_usd)
            # Spam uses floor and sales, so classify after the market lookup.
            if opensea and listed_budget > 0 and spam_reason(row) is None:
                await _listing(client, row, opensea)
                listed_budget -= 1
                native_usd = native.get(NFT_CHAINS[row["chain"]]["native_id"])
                apply_market(row, None, native_usd)
            finalize(row)
    listing_note = (
        "Con una chiave OpenSea nelle impostazioni si vede anche se il pezzo è già in vendita. "
        "Senza chiave quella riga resta «non disponibile»."
        if not opensea
        else "Chiave OpenSea attiva: la quotazione è letta solo in consultazione."
    )
    return {
        "owner": owner,
        "source": source if alchemy_key != ALCHEMY_DEMO_KEY else "alchemy-demo+coingecko",
        "readonly": True,
        "truncated": True,
        "limit_note": (
            f"Al massimo {PAGE_SIZE} NFT per indirizzo e per rete "
            "(Ethereum, BNB, Polygon, Arbitrum, Base). Senza chiave personale si usa la fonte pubblica."
        ),
        "errors": errors,
        "listing_note": listing_note,
        "items": items,
    }
