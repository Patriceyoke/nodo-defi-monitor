"""Monitor DeFi: read-only dashboard, REST API and MCP server."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import bitcoin, config, db, nft, security
from .config import APP_NAME, APP_VERSION
from .mcp_server import router as mcp_router
from .portfolio import cached_snapshot, failed_view, invalidate, refresh_status, snapshot
from .validation import RejectedSecretError, classify_btc_key, normalize_eth, normalize_owner

WEB_DIR = Path(__file__).resolve().parents[1] / "web"
_log = logging.getLogger("app")

app = FastAPI(title=APP_NAME, version=APP_VERSION, docs_url="/api/docs", redoc_url=None)
app.include_router(mcp_router)


def prepare_runtime() -> None:
    """Open the database. A failure here must not stop the HTTP server."""
    try:
        config.ensure_data_dir()
        db.init_db()
        _log.info("Database ready at %s (persistent=%s)", config.DB_PATH, config.DATA_PERSISTENT)
    except Exception:
        _log.exception(
            "Database startup failed. The server will stay up so Umbrel can open the UI, "
            "but saved wallets and settings are unavailable."
        )


@app.on_event("startup")
async def _startup() -> None:
    prepare_runtime()
    asyncio.create_task(security.daily_loop())


class EvmIn(BaseModel):
    label: str = Field(min_length=1, max_length=80)
    address: str
    owner: str


class BtcIn(BaseModel):
    label: str = Field(min_length=1, max_length=80)
    descriptor: str
    owner: str
    role: str = "watch"
    alert_out_btc: float | None = None
    alert_min_btc: float | None = None
    hot_threshold_btc: float | None = None


class SettingsIn(BaseModel):
    eth_rpc_url: str | None = None
    electrs_host: str | None = None
    electrs_port: int | None = None
    currency: str | None = None
    fx_thb: float | None = None
    gas_estimate_usd: float | None = None
    tor_enabled: bool | None = None
    nft_api_provider: str | None = None
    nft_api_key: str | None = None
    btc_gap_limit: int | None = None
    outflow_drop_pct: float | None = None
    outflow_usd: float | None = None
    positions_open: bool | None = None
    rotate_token: bool = False


def _looks_like_wallet_secret(text: str) -> bool:
    from .validation import looks_like_secret
    # A 64-hex API key is not a wallet secret in this field; mnemonics and xprv are.
    if looks_like_secret(text) and not __import__("re").fullmatch(r"(0x)?[0-9a-fA-F]{64}", text.strip()):
        return True
    return False


def _mask_secret(token: str) -> str:
    if len(token) > 8:
        return token[:4] + "…" + token[-4:]
    return "…" if token else ""


def _http_secret(exc: Exception) -> HTTPException:
    if isinstance(exc, RejectedSecretError):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {
        "ok": True,
        "name": APP_NAME,
        "version": APP_VERSION,
        "readonly": True,
        "data_persistent": config.DATA_PERSISTENT,
    }


@app.get("/api/settings")
async def get_settings() -> dict[str, Any]:
    raw = db.all_settings()
    token = raw.get("mcp_token") or ""
    masked = (token[:4] + "…" + token[-4:]) if len(token) > 8 else "…"
    return {
        "eth_rpc_url": raw.get("eth_rpc_url") or "",
        "electrs_host": raw.get("electrs_host") or "",
        "electrs_port": int(raw.get("electrs_port") or 50001),
        "btc_gap_limit": int(raw.get("btc_gap_limit") or 20),
        "outflow_drop_pct": float(raw.get("outflow_drop_pct") or 5),
        "outflow_usd": float(raw.get("outflow_usd") or 500),
        "currency": raw.get("currency") if raw.get("currency") in {"USD", "THB"} else "USD",
        "fx_thb": float(raw["fx_thb"]) if raw.get("fx_thb") else None,
        "gas_estimate_usd": raw.get("gas_estimate_usd") or "",
        "tor_enabled": raw.get("tor_enabled") or "",
        "nft_api_provider": raw.get("nft_api_provider") or "",
        "nft_api_key_set": bool(raw.get("nft_api_key")),
        "nft_api_key_masked": _mask_secret(raw.get("nft_api_key") or ""),
        "mcp_token_masked": masked,
        "has_token": bool(token),
        "positions_open": (raw.get("positions_open") or "1") != "0",
        "readonly": True,
    }


@app.put("/api/settings")
async def put_settings(body: SettingsIn) -> dict[str, Any]:
    if body.eth_rpc_url is not None:
        db.set_setting("eth_rpc_url", body.eth_rpc_url.strip())
    if body.electrs_host is not None:
        db.set_setting("electrs_host", body.electrs_host.strip())
    if body.electrs_port is not None:
        db.set_setting("electrs_port", str(int(body.electrs_port)))
    if body.btc_gap_limit is not None:
        gap = int(body.btc_gap_limit)
        if gap < 1 or gap > 100:
            raise HTTPException(400, "Il gap limit deve essere tra 1 e 100.")
        db.set_setting("btc_gap_limit", str(gap))
    if body.outflow_drop_pct is not None:
        drop = float(body.outflow_drop_pct)
        if drop <= 0 or drop > 100:
            raise HTTPException(400, "La soglia di calo deve essere tra 0 e 100.")
        db.set_setting("outflow_drop_pct", str(drop))
    if body.outflow_usd is not None:
        if body.outflow_usd < 0:
            raise HTTPException(400, "La soglia in dollari non può essere negativa.")
        db.set_setting("outflow_usd", str(body.outflow_usd))
    if body.positions_open is not None:
        db.set_setting("positions_open", "1" if body.positions_open else "0")
    if body.currency is not None:
        cur = body.currency.upper()
        if cur not in {"USD", "THB"}:
            raise HTTPException(400, "Valuta non valida")
        db.set_setting("currency", cur)
    if body.fx_thb is not None and body.fx_thb > 0:
        db.set_setting("fx_thb", str(body.fx_thb))
    if body.gas_estimate_usd is not None:
        db.set_setting("gas_estimate_usd", "" if body.gas_estimate_usd <= 0 else str(body.gas_estimate_usd))
    if body.tor_enabled is not None:
        db.set_setting("tor_enabled", "1" if body.tor_enabled else "0")
    if body.nft_api_provider is not None:
        provider = body.nft_api_provider.strip().lower()
        if provider not in {"", "alchemy", "opensea"}:
            raise HTTPException(400, "Fonte NFT non valida")
        db.set_setting("nft_api_provider", provider)
    if body.nft_api_key is not None:
        key = body.nft_api_key.strip()
        if key and _looks_like_wallet_secret(key):
            raise HTTPException(400, "La chiave API non può essere una seed o una chiave privata.")
        db.set_setting("nft_api_key", key)
    token = None
    if body.rotate_token:
        token = db.rotate_mcp_token()
    out = await get_settings()
    if token:
        out["mcp_token"] = token
        out["note"] = "Copia il token ora: verrà mostrato solo questa volta."
    return out


class NftHideIn(BaseModel):
    chain: str
    contract: str
    token_id: str = ""
    kind: str = "token"


def _nft_hide_target(body: NftHideIn) -> tuple[str, str, str, str]:
    chain = body.chain.strip().lower()
    if chain not in nft.NFT_CHAINS:
        raise ValueError("Catena NFT non prevista.")
    contract = body.contract.strip()
    if not contract.lower().startswith("0x") or len(contract) != 42:
        raise ValueError("Contratto NFT non valido.")
    kind = "collection" if body.kind == "collection" else "token"
    token_id = "" if kind == "collection" else str(body.token_id or "").strip()
    if kind == "token" and not token_id:
        raise ValueError("Manca l'identificativo del pezzo.")
    return chain, contract.lower(), token_id, kind


@app.get("/api/nfts")
async def get_nfts(owner: str = Query("tutti"), refresh: bool = Query(False)) -> dict[str, Any]:
    try:
        return await nft.collect_nfts(owner, force=refresh)
    except Exception as exc:
        raise _http_secret(exc) from exc


@app.post("/api/nfts/hide")
async def hide_nft(body: NftHideIn) -> dict[str, Any]:
    try:
        chain, contract, token_id, kind = _nft_hide_target(body)
        db.hide_nft(chain, contract, token_id, kind)
        db.cache_delete_prefix("nfts:")
    except Exception as exc:
        raise _http_secret(exc) from exc
    return {"ok": True}


@app.post("/api/nfts/unhide")
async def unhide_nft(body: NftHideIn) -> dict[str, Any]:
    try:
        chain, contract, token_id, kind = _nft_hide_target(body)
        db.unhide_nft(chain, contract, token_id, kind)
        db.cache_delete_prefix("nfts:")
    except Exception as exc:
        raise _http_secret(exc) from exc
    return {"ok": True}


@app.get("/api/backup")
async def download_backup() -> dict[str, Any]:
    return db.export_backup()


@app.post("/api/backup")
async def upload_backup(body: dict[str, Any]) -> dict[str, Any]:
    try:
        cleaned = _clean_backup(body)
        result = db.import_backup(cleaned)
    except Exception as exc:
        raise _http_secret(exc) from exc
    return {"ok": True, **result}


def _clean_backup(body: dict[str, Any]) -> dict[str, Any]:
    from .validation import classify_btc_key, normalize_eth, normalize_owner

    if not isinstance(body, dict):
        raise ValueError("Il file non è un oggetto JSON.")
    evm_rows = []
    for row in body.get("evm") or []:
        if not isinstance(row, dict):
            continue
        evm_rows.append(
            {
                "label": str(row.get("label") or "Importato")[:80],
                "address": normalize_eth(str(row.get("address") or "")),
                "owner": normalize_owner(str(row.get("owner") or "")),
            }
        )
    btc_rows = []
    for row in body.get("btc") or []:
        if not isinstance(row, dict):
            continue
        descriptor = bitcoin.prepare_watch_descriptor(str(row.get("descriptor") or ""))
        kind = classify_btc_key(descriptor)
        bitcoin.derive_addresses(descriptor, gap=1)
        btc_rows.append(
            {
                "label": str(row.get("label") or "Importato")[:80],
                "descriptor": descriptor,
                "owner": normalize_owner(str(row.get("owner") or "")),
                "kind": kind,
                "role": str(row.get("role") or "watch"),
                "alert_out_btc": row.get("alert_out_btc"),
                "alert_min_btc": row.get("alert_min_btc"),
                "hot_threshold_btc": row.get("hot_threshold_btc"),
            }
        )
    settings = body.get("settings") if isinstance(body.get("settings"), dict) else {}
    return {"evm": evm_rows, "btc": btc_rows, "settings": settings}


@app.get("/api/wallets")
async def wallets() -> dict[str, Any]:
    evm_rows = db.list_evm()
    btc_rows = []
    for w in db.list_btc():
        item = dict(w)
        item["descriptor_masked"] = bitcoin.mask_key(item.pop("descriptor"))
        btc_rows.append(item)
    return {"evm": evm_rows, "btc": btc_rows}


@app.post("/api/wallets/evm")
async def add_evm_wallet(body: EvmIn) -> dict[str, Any]:
    try:
        owner = normalize_owner(body.owner)
        address = normalize_eth(body.address)
    except Exception as exc:
        raise _http_secret(exc) from exc
    try:
        wid = db.add_evm(body.label.strip(), address, owner)
    except Exception as exc:
        raise HTTPException(status_code=409, detail=f"Non aggiunto: {exc}") from exc
    invalidate()
    return {"id": wid, "ok": True}


@app.post("/api/wallets/btc")
async def add_btc_wallet(body: BtcIn) -> dict[str, Any]:
    try:
        owner = normalize_owner(body.owner)
        descriptor = bitcoin.prepare_watch_descriptor(body.descriptor)
        kind = classify_btc_key(descriptor)
        bitcoin.derive_addresses(descriptor, gap=1)
    except Exception as exc:
        raise _http_secret(exc) from exc
    role = (body.role or "watch").strip().lower()
    if role not in {"watch", "cold", "freddo", "spesa", "hot", "multisig"}:
        role = "watch"
    try:
        wid = db.add_btc(
            body.label.strip(),
            kind,
            descriptor,
            owner,
            role=role,
            alert_out_btc=body.alert_out_btc,
            alert_min_btc=body.alert_min_btc,
            hot_threshold_btc=body.hot_threshold_btc,
        )
    except Exception as exc:
        raise HTTPException(status_code=409, detail=f"Non aggiunto: {exc}") from exc
    invalidate()
    return {"id": wid, "ok": True, "kind": kind}


@app.delete("/api/wallets/evm/{wallet_id}")
async def remove_evm(wallet_id: int) -> dict[str, Any]:
    if not db.delete_evm(wallet_id):
        raise HTTPException(404, "Portafoglio non trovato")
    invalidate()
    return {"ok": True}


@app.delete("/api/wallets/btc/{wallet_id}")
async def remove_btc(wallet_id: int) -> dict[str, Any]:
    if not db.delete_btc(wallet_id):
        raise HTTPException(404, "Portafoglio non trovato")
    invalidate()
    return {"ok": True}


@app.get("/api/portfolio")
async def get_portfolio(
    owner: str = Query("tutti"),
    refresh: bool = Query(False),
) -> dict[str, Any]:
    try:
        return await snapshot(owner, force=refresh)
    except Exception as exc:
        raise _http_secret(exc) from exc


@app.get("/api/balances")
async def get_balances(owner: str = Query("tutti")) -> dict[str, Any]:
    data = await snapshot(owner)
    return {"owner": owner, "positions": data["positions"], "totals": data["totals"]}


@app.get("/api/apys")
async def get_apys(owner: str = Query("tutti")) -> dict[str, Any]:
    data = await snapshot(owner)
    return {
        "owner": owner,
        "apys": [
            {
                "owner": p["owner"],
                "protocol": p["protocol"],
                "asset": p["asset"],
                "usd": p.get("usd"),
                "apy": p.get("apy"),
                "apy30": p.get("apy30"),
                "risk": p.get("risk"),
            }
            for p in data["positions"]
        ],
    }


@app.get("/api/check_switch")
async def check_switch(owner: str = Query("tutti")) -> dict[str, Any]:
    data = await snapshot(owner)
    return {
        "owner": owner,
        "gas_usd": data.get("gas_usd"),
        "switches": [item for item in data["switches"] if not item.get("hidden")],
        "note": "Sono solo proposte. Nessuna transazione parte da qui.",
    }


class SwitchHideIn(BaseModel):
    owner: str
    from_position: str
    to_target: str
    spread: float | None = None


def _switch_target(body: SwitchHideIn) -> tuple[str, str, str]:
    owner = normalize_owner(body.owner)
    source = (body.from_position or "").strip()
    target = (body.to_target or "").strip()
    if not source or not target or len(source) > 80 or len(target) > 80:
        raise ValueError("Suggerimento non valido.")
    return owner, source, target


def _switch_spread(owner: str, source: str, target: str, fallback: float | None) -> float:
    data = cached_snapshot() or {}
    for idea in data.get("switches") or []:
        if idea.get("owner") == owner and idea.get("from") == source and idea.get("to") == target:
            return float(idea["to_apy"]) - float(idea["from_apy"])
    if fallback is None:
        raise ValueError("Suggerimento non trovato.")
    return float(fallback)


@app.post("/api/switches/hide")
async def hide_switch(body: SwitchHideIn) -> dict[str, Any]:
    try:
        owner, source, target = _switch_target(body)
        db.hide_switch(owner, source, target, _switch_spread(owner, source, target, body.spread))
    except Exception as exc:
        raise _http_secret(exc) from exc
    return {"ok": True}


@app.post("/api/switches/unhide")
async def unhide_switch(body: SwitchHideIn) -> dict[str, Any]:
    try:
        owner, source, target = _switch_target(body)
        db.unhide_switch(owner, source, target)
    except Exception as exc:
        raise _http_secret(exc) from exc
    return {"ok": True}


@app.get("/api/alerts")
async def get_alerts(owner: str = Query("tutti")) -> dict[str, Any]:
    """Always 200. Does not start a scan; open it in the browser after Aggiorna."""
    if cached_snapshot() is None:
        status = refresh_status()
        return {
            "ok": True,
            "owner": owner,
            "errors": status.get("errors") or ["Nessun ricalcolo ancora. Premi Aggiorna sulla dashboard."],
            "protocol_security": [],
            "btc_security": [],
            "ens": [],
            "security_summary": security.security_summary(owner),
        }
    try:
        data = await snapshot(owner, force=False)
    except Exception as exc:  # noqa: BLE001
        _log.exception("alerts failed")
        return {
            "ok": False,
            "owner": owner,
            "errors": [f"Avvisi non calcolati: {exc}"],
            "protocol_security": [],
            "btc_security": [],
            "ens": [],
            "security_summary": {"approvals": 0, "hidden": 0, "incidents": 0, "outflows": 0, "visible": 0},
        }
    return {
        "ok": True,
        "owner": owner,
        "errors": data.get("errors") or [],
        "protocol_security": data.get("security") or [],
        "btc_security": data.get("btc_security") or [],
        "ens": data.get("ens_alerts") or [],
        "security_summary": data.get("security_summary") or security.security_summary(owner),
    }


@app.get("/api/history")
async def get_history(owner: str = Query("tutti"), days: int = Query(90, ge=1, le=365)) -> dict[str, Any]:
    data = await snapshot(owner)
    cutoff = data["ts"] - days * 86400
    return {"owner": owner, "days": days, "history": [h for h in data["history"] if int(h.get("ts") or 0) >= cutoff]}


@app.get("/api/btc/wallets")
async def get_btc(owner: str = Query("tutti")) -> dict[str, Any]:
    data = await snapshot(owner)
    return {"owner": owner, "node": data["node"], "btc_price_usd": data["btc_price_usd"], "wallets": data["btc_wallets"], "moves": data["btc_moves"]}


@app.get("/api/security")
async def get_security(owner: str = Query("tutti"), refresh: bool = Query(False)) -> dict[str, Any]:
    """Cached checks. refresh runs them now; the dashboard does not call this on startup."""
    if refresh:
        await security.start_scan()
    return security.public_report(owner)


class ApprovalHideIn(BaseModel):
    wallet: str
    chain: str
    token: str
    spender: str
    allowance: str | None = None


def _approval_target(body: ApprovalHideIn) -> tuple[str, str, str, str, str]:
    wallet = (body.wallet or "").strip().lower()
    token = (body.token or "").strip().lower()
    spender = (body.spender or "").strip().lower()
    chain = (body.chain or "").strip().lower()
    if chain not in security.WATCH_CHAINS:
        raise ValueError("Rete non riconosciuta.")
    if not all(len(item) == 42 and item.startswith("0x") for item in (wallet, token, spender)):
        raise ValueError("Indirizzo non valido.")
    return wallet, chain, token, spender, str(body.allowance if body.allowance is not None else "")


@app.post("/api/security/hide")
async def hide_approval(body: ApprovalHideIn) -> dict[str, Any]:
    try:
        wallet, chain, token, spender, allowance = _approval_target(body)
        db.hide_approval(wallet, chain, token, spender, allowance)
    except Exception as exc:
        raise _http_secret(exc) from exc
    return {"ok": True, "summary": security.security_summary("tutti")}


@app.post("/api/security/unhide")
async def unhide_approval(body: ApprovalHideIn) -> dict[str, Any]:
    try:
        wallet, chain, token, spender, _allowance = _approval_target(body)
        db.unhide_approval(wallet, chain, token, spender)
    except Exception as exc:
        raise _http_secret(exc) from exc
    return {"ok": True, "summary": security.security_summary("tutti")}


@app.post("/api/security/refresh")
async def refresh_security() -> dict[str, Any]:
    security.start_scan()
    return {"ok": True, "running": True, "readonly": True}


@app.get("/api/btc/security")
async def get_btc_sec(owner: str = Query("tutti")) -> dict[str, Any]:
    data = await snapshot(owner)
    return {"owner": owner, "node": data["node"], "checks": data["btc_security"]}


@app.post("/api/refresh")
async def refresh_now(owner: str = Query("tutti"), full: bool = Query(False)) -> dict[str, Any]:
    """Force a portfolio read. full=1 also drops the lazy NFT cache."""
    try:
        data = await snapshot(owner, force=True, fresh_nfts=full)
    except Exception as exc:  # noqa: BLE001
        _log.exception("refresh failed")
        return failed_view(owner, [f"Ricalcolo non riuscito: {exc}"])
    data["ok"] = not data.get("errors")
    return data


# Routes a browser can open. Kept in sync with the decorators below.
API_ROUTES = [
    "GET /api",
    "GET /api/health",
    "GET /api/settings",
    "PUT /api/settings",
    "GET /api/wallets",
    "POST /api/wallets/evm",
    "POST /api/wallets/btc",
    "DELETE /api/wallets/evm/{id}",
    "DELETE /api/wallets/btc/{id}",
    "GET /api/portfolio?owner=tutti&refresh=true",
    "POST /api/refresh?owner=tutti",
    "GET /api/alerts",
    "GET /api/security",
    "POST /api/security/refresh",
    "POST /api/security/hide",
    "POST /api/security/unhide",
    "GET /api/debug/refresh-status",
    "GET /api/balances",
    "GET /api/apys",
    "GET /api/check_switch",
    "POST /api/switches/hide",
    "POST /api/switches/unhide",
    "GET /api/history",
    "GET /api/btc/wallets",
    "GET /api/btc/security",
    "GET /api/nfts",
    "POST /api/nfts/hide",
    "POST /api/nfts/unhide",
    "GET /api/backup",
    "POST /api/backup",
    "POST /mcp",
]


@app.get("/api")
async def api_index() -> dict[str, Any]:
    return {
        "name": APP_NAME,
        "version": APP_VERSION,
        "readonly": True,
        "routes": API_ROUTES,
        "note": "GET /api/alerts risponde subito con l'ultimo ricalcolo. POST /api/refresh forza la lettura. GET /api/debug/refresh-status mostra RPC e errori per indirizzo.",
    }


@app.get("/api/debug/refresh-status")
async def debug_refresh_status() -> dict[str, Any]:
    """Last refresh only. Does not start a new scan."""
    return refresh_status()


if WEB_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")
