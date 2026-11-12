"""Assemble the portfolio view: DeFi positions, Bitcoin, history, alerts, switches."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from . import btc_security, bitcoin, db, evm, markets, switches
from .config import PROTOCOL_RISK
from .validation import normalize_owner

log = logging.getLogger("monitor.portfolio")

# In-process cache so the UI and MCP can share a recent snapshot.
_SNAPSHOT: dict[str, Any] | None = None
_SNAPSHOT_AT = 0.0
_LOCK = asyncio.Lock()
REFRESH_TTL = 60


def filter_owner(owner: str, item_owner: str) -> bool:
    return owner == "tutti" or owner == item_owner


def stats(positions: list[dict[str, Any]]) -> dict[str, float]:
    # Wallet ETH is included in portfolio totals, but not in the APY average.
    # Positions with an unknown APY stay in the total and are left out of the average
    # so a missing rate is not treated as 0%.
    defi = [p for p in positions if p.get("protocol") != "Wallet"]
    defi_usd = sum(float(p.get("usd") or 0) for p in defi)
    known = [p for p in defi if p.get("apy") is not None and p.get("usd")]
    known_usd = sum(float(p.get("usd") or 0) for p in known)
    year = sum(float(p.get("usd") or 0) * float(p["apy"]) / 100.0 for p in known)
    wapy = (year / known_usd * 100.0) if known_usd else 0.0
    wallet_usd = sum(float(p.get("usd") or 0) for p in positions if p.get("protocol") == "Wallet")
    return {
        "total_usd": defi_usd + wallet_usd,
        "defi_usd": defi_usd,
        "wallet_usd": wallet_usd,
        "year_usd": year,
        "wapy": wapy,
    }


def _lookup_price(px: dict[str, float], row: dict[str, Any]) -> float:
    if row.get("valuation_ok") is False:
        return 0.0
    if row.get("asset") == "ETH":
        return float(px.get("ETH") or 0)
    key = row.get("price_token") or ""
    if not key:
        return 0.0
    if key in px:
        return float(px[key])
    return float(px.get(key.lower()) or 0)


async def _enrich(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    """Attach price and APY. Never drop a position because either lookup failed."""
    alerts: list[str] = []
    tokens = ["ETH"]
    for row in rows:
        token = row.get("price_token")
        if token:
            tokens.append(token)
    try:
        px = await markets.prices_usd(list(dict.fromkeys(tokens)))
    except Exception as exc:  # noqa: BLE001
        log.warning("Price feed failed: %s", exc)
        px = {}
        alerts.append(f"Listino prezzi non disponibile: {exc}. Le posizioni restano in elenco senza dollari.")
    out = []
    missing_price: list[str] = []
    missing_apy: list[str] = []
    for row in rows:
        name = f"{row.get('protocol')} {row.get('asset')}"
        price = _lookup_price(px, row)
        apy, apy30, pool = (None, None, None)
        if row.get("protocol") != "Wallet":
            try:
                apy, apy30, pool = await markets.apy_for(
                    row.get("llama_project"),
                    row.get("llama_symbol") or row.get("asset"),
                    row.get("chain") or "ethereum",
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("APY lookup failed for %s: %s", name, exc)
                apy, apy30, pool = None, None, None
        if price <= 0:
            usd = None
            price_out = None
            if row.get("valuation_ok") is not False:
                missing_price.append(name)
                log.warning("Prezzo non disponibile per %s", name)
        else:
            usd = float(row["amount"]) * float(price)
            price_out = price
            # Hide leftover dust only when we actually know the dollar value.
            if usd < 1:
                continue
        item = {
            **row,
            "price_usd": price_out,
            "usd": usd,
            "apy": apy,
            "apy30": apy30 if apy30 is not None else apy,
            "apy_available": apy is not None if row.get("protocol") != "Wallet" else None,
            "risk": PROTOCOL_RISK.get(row["protocol"], "medio"),
            "pool_id": (pool or {}).get("pool"),
        }
        if row.get("protocol") != "Wallet" and apy is None:
            missing_apy.append(name)
            log.warning("APY non disponibile per %s", name)
        out.append(item)
    if missing_price:
        alerts.append(
            "Prezzo non disponibile, posizione comunque in elenco: " + ", ".join(missing_price)
        )
    if missing_apy:
        alerts.append(
            "APY non disponibile, posizione comunque in elenco: " + ", ".join(missing_apy)
        )
    return out, alerts


async def refresh(force: bool = False) -> dict[str, Any]:
    global _SNAPSHOT, _SNAPSHOT_AT
    async with _LOCK:
        now = time.time()
        if _SNAPSHOT and not force and now - _SNAPSHOT_AT < REFRESH_TTL:
            return _SNAPSHOT
        evm_wallets = db.list_evm()
        btc_wallets = db.list_btc()
        positions: list[dict[str, Any]] = []
        errors: list[str] = []
        chains_ok: dict[str, bool] = {}
        from .protocols import CHAINS
        for chain_id, chain in CHAINS.items():
            if not chain.get("scan_balances", True):
                continue
            try:
                await evm.eth_rpc("eth_blockNumber", [], chain=chain_id)
                chains_ok[chain_id] = True
            except Exception as exc:  # noqa: BLE001
                chains_ok[chain_id] = False
                msg = f"RPC {chain['label']} non raggiungibile: {exc}"
                log.warning(msg)
                errors.append(msg)
        rpc_ok = any(chains_ok.values())

        if rpc_ok:
            for w in evm_wallets:
                try:
                    found, notes = await evm.scan_wallet(w["address"], w["owner"], w["label"], chains_ok)
                    positions.extend(found)
                    errors.extend(notes)
                except Exception as exc:  # noqa: BLE001
                    log.warning("Lettura %s fallita: %s", w["label"], exc)
                    errors.append(f"Lettura {w['label']}: {exc}")
            try:
                positions, alerts = await _enrich(positions)
                errors.extend(alerts)
            except Exception as exc:  # noqa: BLE001
                log.warning("Arricchimento prezzi/APY fallito: %s", exc)
                errors.append(f"Prezzi o APY: {exc}. Le posizioni lette restano in elenco.")
                positions = [{**p, "price_usd": None, "usd": None, "apy": None, "apy30": None, "risk": PROTOCOL_RISK.get(p["protocol"], "medio")} for p in positions]

        node = await bitcoin.electrs_status()
        btc_scans: list[dict[str, Any]] = []
        for w in btc_wallets:
            try:
                btc_scans.append(await bitcoin.scan_wallet(w))
            except Exception as exc:  # noqa: BLE001
                errors.append(f"Bitcoin {w['label']}: {exc}")
                btc_scans.append(
                    {
                        "id": w["id"],
                        "label": w["label"],
                        "owner": w["owner"],
                        "kind": w["kind"],
                        "role": w.get("role") or "watch",
                        "key_masked": bitcoin.mask_key(w["descriptor"]),
                        "btc": 0,
                        "sats": 0,
                        "status": "Errore",
                        "last": str(exc),
                        "connected": False,
                        "addresses": [],
                        "txs": [],
                        "reuse": [],
                        "dust": [],
                        "alert_out_btc": w.get("alert_out_btc"),
                        "alert_min_btc": w.get("alert_min_btc"),
                        "hot_threshold_btc": w.get("hot_threshold_btc"),
                        "node": node,
                    }
                )

        btc_price = await markets.btc_price_usd()
        fx = await markets.fx_eur()
        eth_price = 0.0
        for p in positions:
            if p.get("asset") == "ETH" and p.get("price_usd"):
                eth_price = float(p["price_usd"])
                break
        if not eth_price:
            try:
                from .protocols import WETH
                px = await markets.prices_usd([WETH])
                eth_price = px.get(f"ethereum:{WETH.lower()}") or px.get("ETH") or 0
            except Exception as exc:  # noqa: BLE001
                log.warning("Prezzo ETH di riserva non disponibile: %s", exc)
                errors.append(f"Prezzo ETH non disponibile: {exc}")
                eth_price = 0

        gas_override = db.get_setting("gas_estimate_usd")
        override = float(gas_override) if gas_override else None
        gas_usd = await switches.gas_usd_now(override, eth_price or 0)
        try:
            alts = await switches.market_alternatives(positions)
        except Exception as exc:  # noqa: BLE001
            alts = []
            errors.append(f"Mercato APY: {exc}")
        pos_with_alts = []
        for p in positions:
            p = dict(p)
            p["alternatives"] = alts
            pos_with_alts.append(p)
        ideas = switches.suggest_switches(pos_with_alts, gas_usd)

        try:
            health = await markets.protocol_health()
        except Exception as exc:  # noqa: BLE001
            health = []
            errors.append(f"Controlli di sicurezza: {exc}")

        tor_raw = db.get_setting("tor_enabled")
        tor_flag = None if tor_raw == "" else tor_raw in {"1", "true", "yes", "on"}
        btc_checks = btc_security.evaluate(btc_scans, node, tor_flag)

        # Persist a compact history point per owner + combined
        def hist_payload(owner: str) -> dict[str, Any]:
            pos = [p for p in positions if filter_owner(owner, p["owner"])]
            btc = [w for w in btc_scans if filter_owner(owner, w["owner"])]
            s = stats(pos)
            btc_amt = sum(float(w.get("btc") or 0) for w in btc)
            btc_usd = btc_amt * (btc_price or 0)
            return {
                "defi_usd": s["defi_usd"] + s["wallet_usd"],
                "year_usd": s["year_usd"],
                "wapy": s["wapy"],
                "btc": btc_amt,
                "btc_usd": btc_usd,
                "total_usd": s["total_usd"] + btc_usd,
            }

        for owner in ("io", "moglie", "tutti"):
            db.save_history(owner, hist_payload(owner))

        snapshot = {
            "ts": int(time.time()),
            "rpc_ok": rpc_ok,
            "errors": errors,
            "positions": positions,
            "switches": ideas,
            "security": health,
            "btc_wallets": btc_scans,
            "btc_security": btc_checks,
            "btc_price_usd": btc_price,
            "fx_eur": fx,
            "gas_usd": gas_usd,
            "eth_price_usd": eth_price,
            "node": node,
            "alternatives": alts,
        }
        _SNAPSHOT = snapshot
        _SNAPSHOT_AT = now
        return snapshot


async def snapshot(owner: str = "tutti", force: bool = False) -> dict[str, Any]:
    owner = normalize_owner(owner, allow_all=True)
    data = await refresh(force=force)
    positions = [p for p in data["positions"] if filter_owner(owner, p["owner"])]
    btc = [w for w in data["btc_wallets"] if filter_owner(owner, w["owner"])]
    ideas = [s for s in data["switches"] if filter_owner(owner, s["owner"])]
    held = {p["protocol"] for p in positions}
    security = [c for c in data["security"] if c.get("protocol") in held or c.get("protocol") in {p.get("asset") for p in positions}]
    # protocol_health tags protocol OR stable name; filter to held protocols mostly
    security = [
        c for c in data["security"]
        if c.get("protocol") in held or (c.get("protocol") in {"USDC", "USDT", "DAI", "USDS"} and any(p.get("asset") == c.get("protocol") for p in positions))
    ]
    btc_sec = [c for c in data["btc_security"] if c.get("owner") == "tutti" or filter_owner(owner, c["owner"])]
    price = data.get("btc_price_usd") or 0
    s = stats(positions)
    btc_amt = sum(float(w.get("btc") or 0) for w in btc)
    btc_usd = btc_amt * price
    # Always include Io/Moglie/Combinato totals
    def totals(filt: str) -> dict[str, Any]:
        pos = [p for p in data["positions"] if filter_owner(filt, p["owner"])]
        bw = [w for w in data["btc_wallets"] if filter_owner(filt, w["owner"])]
        st = stats(pos)
        amt = sum(float(w.get("btc") or 0) for w in bw)
        usd = amt * price
        return {
            "owner": filt,
            "defi_usd": st["defi_usd"] + st["wallet_usd"],
            "btc": amt,
            "btc_usd": usd,
            "total_usd": st["total_usd"] + usd,
            "wapy": st["wapy"],
            "year_usd": st["year_usd"],
        }

    history = db.get_history(owner, days=90)
    txs = []
    for w in btc:
        for tx in w.get("txs") or []:
            txs.append(tx)
    txs.sort(key=lambda t: t.get("height") or 0, reverse=True)

    return {
        "owner": owner,
        "ts": data["ts"],
        "rpc_ok": data["rpc_ok"],
        "errors": data["errors"],
        "fx_eur": data["fx_eur"],
        "gas_usd": data["gas_usd"],
        "eth_price_usd": data["eth_price_usd"],
        "btc_price_usd": data["btc_price_usd"],
        "node": data["node"],
        "totals": {
            "io": totals("io"),
            "moglie": totals("moglie"),
            "tutti": totals("tutti"),
            "view": totals(owner),
        },
        "positions": positions,
        "switches": ideas,
        "security": security,
        "btc_wallets": [
            {k: v for k, v in w.items() if k not in {"addresses"}} | {"address_count": len(w.get("addresses") or [])}
            for w in btc
        ],
        "btc_security": btc_sec,
        "btc_moves": txs[:40],
        "history": history,
    }
