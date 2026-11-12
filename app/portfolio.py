"""Assemble the portfolio view: DeFi positions, Bitcoin, history, alerts, switches."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from . import btc_security, bitcoin, db, ens, evm, markets, switches
from .config import PROTOCOL_RISK
from .validation import normalize_owner

log = logging.getLogger("monitor.portfolio")

# In-process cache so the UI and MCP can share a recent snapshot.
_SNAPSHOT: dict[str, Any] | None = None
_SNAPSHOT_AT = 0.0
_LOCK = asyncio.Lock()
REFRESH_TTL = 60
_STATUS: dict[str, Any] = {
    "last_started_at": None,
    "last_finished_at": None,
    "last_ok": None,
    "last_error": None,
    "duration_s": None,
    "rpc": {},
    "wallets": [],
    "errors": [],
    "position_count": 0,
    "wallet_count": 0,
}


def cached_snapshot() -> dict[str, Any] | None:
    """Last snapshot in memory, or the one saved on disk after a restart."""
    global _SNAPSHOT, _SNAPSHOT_AT
    if _SNAPSHOT is None:
        stored = db.cache_get("portfolio")
        if isinstance(stored, dict) and "positions" in stored:
            _SNAPSHOT = stored
            _SNAPSHOT_AT = float(stored.get("ts") or stored.get("cached_at") or 0)
    return _SNAPSHOT


def invalidate() -> None:
    """Drop the cached snapshot so the next read scans the wallets just saved."""
    global _SNAPSHOT, _SNAPSHOT_AT
    _SNAPSHOT = None
    _SNAPSHOT_AT = 0.0


def refresh_status() -> dict[str, Any]:
    """Last refresh, without starting a new one. Safe to open in a browser."""
    from .evm import rpc_status

    return {
        "last_started_at": _STATUS.get("last_started_at"),
        "last_finished_at": _STATUS.get("last_finished_at"),
        "last_ok": _STATUS.get("last_ok"),
        "last_error": _STATUS.get("last_error"),
        "duration_s": _STATUS.get("duration_s"),
        "rpc": _STATUS.get("rpc") or rpc_status(),
        "wallets": _STATUS.get("wallets") or [],
        "errors": _STATUS.get("errors") or [],
        "position_count": _STATUS.get("position_count") or 0,
        "wallet_count": _STATUS.get("wallet_count") or 0,
    }


def _zero_total(owner: str) -> dict[str, Any]:
    return {
        "owner": owner,
        "defi_usd": 0.0,
        "btc": 0.0,
        "btc_usd": 0.0,
        "total_usd": 0.0,
        "wapy": 0.0,
        "wapy30": 0.0,
        "year_usd": 0.0,
    }


def failed_view(owner: str, errors: list[str]) -> dict[str, Any]:
    """A complete portfolio payload so the UI can render and show the banner."""
    return {
        "ok": False,
        "owner": owner,
        "ts": int(time.time()),
        "rpc_ok": False,
        "errors": errors,
        "fx_thb": None,
        "currency": "USD",
        "income": {
            "io": {"week_usd": 0.0, "month_usd": 0.0, "year_usd": 0.0, "apy_year": 0.0, "apy_month": 0.0},
            "moglie": {"week_usd": 0.0, "month_usd": 0.0, "year_usd": 0.0, "apy_year": 0.0, "apy_month": 0.0},
            "tutti": {"week_usd": 0.0, "month_usd": 0.0, "year_usd": 0.0, "apy_year": 0.0, "apy_month": 0.0},
            "view": {"week_usd": 0.0, "month_usd": 0.0, "year_usd": 0.0, "apy_year": 0.0, "apy_month": 0.0},
            "by_protocol": {"io": [], "moglie": [], "tutti": []},
        },
        "gas_usd": 0.0,
        "eth_price_usd": 0.0,
        "btc_price_usd": None,
        "node": {"configured": False, "connected": False, "detail": "Ricalcolo non completato."},
        "totals": {
            "io": _zero_total("io"),
            "moglie": _zero_total("moglie"),
            "tutti": _zero_total("tutti"),
            "view": _zero_total(owner),
        },
        "positions": [],
        "switches": [],
        "security": [],
        "btc_wallets": [],
        "btc_security": [],
        "btc_moves": [],
        "history": [],
        "ens_alerts": [],
        "ens_names": [],
        "security_summary": {
            "io": {"approvals": 0, "hidden": 0, "incidents": 0, "outflows": 0, "visible": 0},
            "moglie": {"approvals": 0, "hidden": 0, "incidents": 0, "outflows": 0, "visible": 0},
            "tutti": {"approvals": 0, "hidden": 0, "incidents": 0, "outflows": 0, "visible": 0},
            "view": {"approvals": 0, "hidden": 0, "incidents": 0, "outflows": 0, "visible": 0},
        },
        "wallet_count": _STATUS.get("wallet_count") or 0,
    }


def _security_summary(owner: str) -> dict[str, Any]:
    empty = {"approvals": 0, "hidden": 0, "incidents": 0, "outflows": 0, "visible": 0}
    try:
        from .security import security_summary
        return {
            "io": security_summary("io"),
            "moglie": security_summary("moglie"),
            "tutti": security_summary("tutti"),
            "view": security_summary(owner),
        }
    except Exception:  # noqa: BLE001
        return {"io": empty, "moglie": empty, "tutti": empty, "view": empty}


def filter_owner(owner: str, item_owner: str) -> bool:
    return owner == "tutti" or owner == item_owner


def monthly_yield_pct(apy_pct: float) -> float:
    """Compound monthly rate, in percent: (1+APY)^(1/12)-1."""
    try:
        apy = float(apy_pct)
    except (TypeError, ValueError):
        return 0.0
    if apy <= -100:
        return 0.0
    return ((1.0 + apy / 100.0) ** (1.0 / 12.0) - 1.0) * 100.0


def income_figures(positions: list[dict[str, Any]]) -> dict[str, float]:
    """Dollar income is the annual APY split into a week, a month and a year."""
    st = stats(positions)
    year = float(st["year_usd"])
    apy = float(st["wapy"])
    return {
        "week_usd": year / 52.0,
        "month_usd": year / 12.0,
        "year_usd": year,
        "apy_year": apy,
        "apy_month": monthly_yield_pct(apy),
    }


def income_by_protocol(positions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, float]] = {}
    for row in positions:
        if row.get("protocol") == "Wallet" or row.get("apy") is None or not row.get("usd"):
            continue
        bucket = grouped.setdefault(str(row["protocol"]), {"usd": 0.0, "year": 0.0})
        usd = float(row["usd"])
        bucket["usd"] += usd
        bucket["year"] += usd * float(row["apy"]) / 100.0
    out = []
    for protocol, bucket in grouped.items():
        apy = (bucket["year"] / bucket["usd"] * 100.0) if bucket["usd"] else 0.0
        out.append({
            "protocol": protocol,
            "usd": bucket["usd"],
            "week_usd": bucket["year"] / 52.0,
            "month_usd": bucket["year"] / 12.0,
            "year_usd": bucket["year"],
            "apy_year": apy,
            "apy_month": monthly_yield_pct(apy),
        })
    out.sort(key=lambda item: item["year_usd"], reverse=True)
    return out


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
    known30 = [p for p in defi if p.get("apy30") is not None and p.get("usd")]
    known30_usd = sum(float(p.get("usd") or 0) for p in known30)
    weighted30 = sum(float(p.get("usd") or 0) * float(p["apy30"]) for p in known30)
    wapy30 = (weighted30 / known30_usd) if known30_usd else 0.0
    wallet_usd = sum(float(p.get("usd") or 0) for p in positions if p.get("protocol") == "Wallet")
    return {
        "total_usd": defi_usd + wallet_usd,
        "defi_usd": defi_usd,
        "wallet_usd": wallet_usd,
        "year_usd": year,
        "wapy": wapy,
        "wapy30": wapy30,
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


def _empty_raw() -> dict[str, Any]:
    return {
        "ts": int(time.time()),
        "rpc_ok": False,
        "errors": [],
        "positions": [],
        "switches": [],
        "security": [],
        "btc_wallets": [],
        "btc_security": [],
        "btc_price_usd": None,
        "fx_thb": None,
        "gas_usd": 0.0,
        "eth_price_usd": 0.0,
        "node": {"configured": False, "connected": False, "detail": "Nessuno snapshot salvato."},
        "alternatives": [],
        "evm_owners": [],
        "wallet_count": 0,
        "ens_names": [],
        "ens_alerts": [],
        "saved": False,
    }


async def refresh(force: bool = False) -> dict[str, Any]:
    global _SNAPSHOT, _SNAPSHOT_AT
    async with _LOCK:
        now = time.time()
        if not force:
            cached_snapshot()
            if _SNAPSHOT:
                return _SNAPSHOT
            return _empty_raw()
        _STATUS["last_started_at"] = time.time()
        _STATUS["last_ok"] = None
        evm_wallets = db.list_evm()
        btc_wallets = db.list_btc()
        positions: list[dict[str, Any]] = []
        errors: list[str] = []
        chains_ok: dict[str, bool] = {}
        from .protocols import CHAINS

        async def _probe(chain_id: str, chain: dict[str, Any]) -> tuple[str, bool, str | None]:
            try:
                await evm.eth_rpc("eth_blockNumber", [], chain=chain_id)
                return chain_id, True, None
            except Exception as exc:  # noqa: BLE001
                return chain_id, False, f"RPC {chain['label']} non raggiungibile: {exc}"

        probes = await asyncio.gather(
            *[_probe(chain_id, chain) for chain_id, chain in CHAINS.items() if chain.get("scan_balances", True)]
        )
        for chain_id, ok, message in probes:
            chains_ok[chain_id] = ok
            if message:
                log.warning(message)
                errors.append(message)
        rpc_ok = any(chains_ok.values())

        from .evm import rpc_status

        wallet_reports: list[dict[str, Any]] = []

        async def _scan_evm(w: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str], dict[str, Any]]:
            try:
                found, notes, reports = await evm.scan_wallet(w["address"], w["owner"], w["label"], chains_ok)
                by_chain: dict[str, list[str]] = {}
                for report in reports:
                    by_chain.setdefault(report["chain"], []).append(f"{report['asset']}: {report['error']}")
                return found, notes, {
                    "label": w["label"],
                    "owner": w["owner"],
                    "address": w["address"],
                    "positions": len(found),
                    "chains": [
                        {"chain": chain, "ok": not msgs, "errors": msgs}
                        for chain, msgs in by_chain.items()
                    ],
                }
            except Exception as exc:  # noqa: BLE001
                log.warning("Lettura %s fallita: %s", w["label"], exc)
                return [], [f"Lettura {w['label']}: {exc}"], {
                    "label": w["label"],
                    "owner": w["owner"],
                    "address": w["address"],
                    "positions": 0,
                    "chains": [{"chain": "ethereum", "ok": False, "errors": [str(exc)]}],
                }

        if rpc_ok and evm_wallets:
            scanned = await asyncio.gather(*[_scan_evm(w) for w in evm_wallets])
            for found, notes, report in scanned:
                positions.extend(found)
                errors.extend(notes)
                wallet_reports.append(report)
        elif evm_wallets:
            for w in evm_wallets:
                wallet_reports.append(
                    {
                        "label": w["label"],
                        "owner": w["owner"],
                        "address": w["address"],
                        "positions": 0,
                        "chains": [
                            {"chain": chain, "ok": False, "errors": ["RPC non raggiungibile"]}
                            for chain, ok in chains_ok.items()
                            if not ok
                        ],
                    }
                )
        if positions:
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
        fx = await markets.fx_thb()
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

        ens_names: list[dict[str, Any]] = []
        if rpc_ok and chains_ok.get("ethereum") and evm_wallets:
            try:
                ens_names = await asyncio.wait_for(ens.scan_wallets(evm_wallets), timeout=12)
            except Exception as exc:  # noqa: BLE001
                log.warning("Scadenze ENS non lette: %s", exc)
                errors.append(f"Scadenze ENS non lette: {exc}")
        ens_alerts = ens.alerts_for(ens_names)

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

        if evm_wallets and not positions:
            errors.append(
                "I portafogli Ethereum sono salvati, ma questo ricalcolo non ha trovato posizioni. "
                "Controlla /api/debug/refresh-status per la rete e l'errore di ogni indirizzo."
            )

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
            "fx_thb": fx,
            "gas_usd": gas_usd,
            "eth_price_usd": eth_price,
            "node": node,
            "alternatives": alts,
            "evm_owners": [w["owner"] for w in evm_wallets],
            "wallet_count": len(evm_wallets) + len(btc_wallets),
            "ens_names": ens_names,
            "ens_alerts": ens_alerts,
            "saved": True,
        }
        _SNAPSHOT = snapshot
        _SNAPSHOT_AT = now
        try:
            db.cache_set("portfolio", snapshot)
        except Exception as exc:  # noqa: BLE001
            log.warning("Snapshot non salvato: %s", exc)
        _STATUS.update(
            {
                "last_finished_at": time.time(),
                "last_ok": True,
                "last_error": None,
                "duration_s": round(time.time() - (_STATUS.get("last_started_at") or time.time()), 3),
                "rpc": rpc_status(),
                "wallets": wallet_reports,
                "errors": list(errors),
                "position_count": len(positions),
                "wallet_count": len(evm_wallets) + len(btc_wallets),
            }
        )
        return snapshot


async def snapshot(owner: str = "tutti", force: bool = False) -> dict[str, Any]:
    owner = normalize_owner(owner, allow_all=True)
    try:
        data = await refresh(force=force)
    except Exception as exc:  # noqa: BLE001
        log.exception("snapshot failed")
        _STATUS["last_ok"] = False
        _STATUS["last_error"] = str(exc)
        _STATUS["last_finished_at"] = time.time()
        _STATUS["errors"] = [f"Ricalcolo non riuscito: {exc}"]
        data = {
            "ts": int(time.time()),
            "rpc_ok": False,
            "errors": [f"Ricalcolo non riuscito: {exc}"],
            "positions": [],
            "switches": [],
            "security": [],
            "btc_wallets": [],
            "btc_security": [],
            "btc_price_usd": None,
            "fx_thb": None,
            "gas_usd": 0.0,
            "eth_price_usd": 0.0,
            "node": {"configured": False, "connected": False, "detail": str(exc)},
            "alternatives": [],
            "evm_owners": [],
            "wallet_count": 0,
            "ens_names": [],
            "ens_alerts": [],
            "saved": False,
        }
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
            "wapy30": st["wapy30"],
            "year_usd": st["year_usd"],
        }

    all_positions = data["positions"]
    income = {
        "io": income_figures([p for p in all_positions if filter_owner("io", p["owner"])]),
        "moglie": income_figures([p for p in all_positions if filter_owner("moglie", p["owner"])]),
        "tutti": income_figures(all_positions),
        "view": income_figures(positions),
        "by_protocol": {
            "io": income_by_protocol([p for p in all_positions if filter_owner("io", p["owner"])]),
            "moglie": income_by_protocol([p for p in all_positions if filter_owner("moglie", p["owner"])]),
            "tutti": income_by_protocol(all_positions),
        },
    }
    for key in ("io", "moglie", "tutti"):
        row = totals(key)
        income[key]["btc"] = row["btc"]
        income[key]["btc_usd"] = row["btc_usd"]
        income[key]["btc_apy"] = 0.0
        income[key]["btc_note"] = "Bitcoin non produce rendimento"
    view_btc = totals(owner)
    income["view"]["btc"] = view_btc["btc"]
    income["view"]["btc_usd"] = view_btc["btc_usd"]
    income["view"]["btc_apy"] = 0.0
    income["view"]["btc_note"] = "Bitcoin non produce rendimento"
    currency = (db.get_setting("currency") or "USD").upper()
    if currency not in {"USD", "THB"}:
        currency = "USD"

    ens_alerts = [a for a in (data.get("ens_alerts") or []) if filter_owner(owner, a.get("owner"))]
    ens_names = [a for a in (data.get("ens_names") or []) if filter_owner(owner, a.get("owner"))]
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
        "fx_thb": data.get("fx_thb"),
        "currency": currency,
        "income": income,
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
            {
                k: v
                for k, v in w.items()
                if k not in {"addresses", "descriptor"}
            }
            | {
                "address_count": len(w.get("addresses") or []),
                "usd": float(w.get("btc") or 0) * price,
            }
            for w in btc
        ],
        "btc_security": btc_sec,
        "btc_moves": txs[:40],
        "history": history,
        "ens_alerts": ens_alerts,
        "ens_names": ens_names,
        "security_summary": _security_summary(owner),
        "saved": data.get("saved") is not False,
        "wallet_count": sum(1 for item in (data.get("evm_owners") or []) if filter_owner(owner, item))
        + sum(1 for w in data.get("btc_wallets") or [] if filter_owner(owner, w.get("owner"))),
        "ok": not data.get("errors"),
    }
