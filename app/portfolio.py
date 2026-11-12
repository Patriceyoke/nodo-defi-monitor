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
    total = sum(float(p.get("usd") or 0) for p in positions if p.get("protocol") != "Wallet" or True)
    # Wallet ETH is included in DeFi/portfolio totals as liquid ETH, but not in APY.
    defi = [p for p in positions if p.get("protocol") != "Wallet"]
    defi_usd = sum(float(p.get("usd") or 0) for p in defi)
    year = sum(float(p.get("usd") or 0) * float(p.get("apy") or 0) / 100.0 for p in defi)
    wapy = (year / defi_usd * 100.0) if defi_usd else 0.0
    wallet_usd = sum(float(p.get("usd") or 0) for p in positions if p.get("protocol") == "Wallet")
    return {
        "total_usd": defi_usd + wallet_usd,
        "defi_usd": defi_usd,
        "wallet_usd": wallet_usd,
        "year_usd": year,
        "wapy": wapy,
    }


async def _enrich(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    tokens = []
    for r in rows:
        token = r.get("price_token") or r.get("token")
        if token:
            tokens.append(token)
    tokens.append("ETH")
    px = await markets.prices_usd(list(dict.fromkeys(tokens)))
    out = []
    for r in rows:
        token = (r.get("price_token") or r.get("token") or "").lower()
        price = px.get(token) or px.get(r.get("token", "").lower()) or 0
        if r.get("asset") == "ETH":
            price = px.get("ETH") or px.get("ethereum:eth".lower()) or price
            # llama key for WETH often works for ETH
            if not price:
                from .protocols import WETH
                price = px.get(WETH.lower()) or 0
        usd = float(r["amount"]) * float(price)
        apy, apy30, pool = (None, None, None)
        if r.get("protocol") != "Wallet":
            apy, apy30, pool = await markets.apy_for(r.get("llama_project"), r.get("llama_symbol") or r.get("asset"))
        item = {
            **r,
            "price_usd": price,
            "usd": usd,
            "apy": apy,
            "apy30": apy30 if apy30 is not None else apy,
            "risk": PROTOCOL_RISK.get(r["protocol"], "medio"),
            "pool_id": (pool or {}).get("pool"),
        }
        # Hide leftover dust so the dashboard stays readable.
        if item["usd"] < 1:
            continue
        out.append(item)
    return out


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
        rpc_ok = True
        try:
            # Probe RPC once
            await evm.eth_rpc("eth_blockNumber", [])
        except Exception as exc:  # noqa: BLE001
            rpc_ok = False
            errors.append(f"RPC Ethereum non raggiungibile: {exc}")

        if rpc_ok:
            for w in evm_wallets:
                try:
                    found = await evm.scan_wallet(w["address"], w["owner"], w["label"])
                    positions.extend(found)
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"Lettura {w['label']}: {exc}")
            try:
                positions = await _enrich(positions)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"Prezzi o APY: {exc}")
                positions = [{**p, "price_usd": 0, "usd": 0, "apy": None, "apy30": None, "risk": PROTOCOL_RISK.get(p["protocol"], "medio")} for p in positions]

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
                eth_price = px.get(WETH.lower()) or 0
            except Exception:
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
