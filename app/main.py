"""Monitor DeFi: read-only dashboard, REST API and MCP server."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import bitcoin, db
from .config import APP_NAME, APP_VERSION
from .mcp_server import router as mcp_router
from .portfolio import snapshot
from .validation import RejectedSecretError, classify_btc_key, normalize_eth, normalize_owner

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
WEB_DIR = Path(__file__).resolve().parents[1] / "web"

app = FastAPI(title=APP_NAME, version=APP_VERSION, docs_url="/api/docs", redoc_url=None)
app.include_router(mcp_router)


@app.on_event("startup")
def _startup() -> None:
    db.init_db()


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
    fx_eur: float | None = None
    gas_estimate_usd: float | None = None
    tor_enabled: bool | None = None
    rotate_token: bool = False


def _http_secret(exc: Exception) -> HTTPException:
    if isinstance(exc, RejectedSecretError):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "name": APP_NAME, "version": APP_VERSION, "readonly": True}


@app.get("/api/settings")
async def get_settings() -> dict[str, Any]:
    raw = db.all_settings()
    token = raw.get("mcp_token") or ""
    masked = (token[:4] + "…" + token[-4:]) if len(token) > 8 else "…"
    return {
        "eth_rpc_url": raw.get("eth_rpc_url") or "",
        "electrs_host": raw.get("electrs_host") or "",
        "electrs_port": int(raw.get("electrs_port") or 50001),
        "currency": raw.get("currency") or "USD",
        "fx_eur": float(raw.get("fx_eur") or 0.92),
        "gas_estimate_usd": raw.get("gas_estimate_usd") or "",
        "tor_enabled": raw.get("tor_enabled") or "",
        "mcp_token_masked": masked,
        "has_token": bool(token),
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
    if body.currency is not None:
        cur = body.currency.upper()
        if cur not in {"USD", "EUR"}:
            raise HTTPException(400, "Valuta non valida")
        db.set_setting("currency", cur)
    if body.fx_eur is not None:
        db.set_setting("fx_eur", str(body.fx_eur))
    if body.gas_estimate_usd is not None:
        db.set_setting("gas_estimate_usd", "" if body.gas_estimate_usd <= 0 else str(body.gas_estimate_usd))
    if body.tor_enabled is not None:
        db.set_setting("tor_enabled", "1" if body.tor_enabled else "0")
    token = None
    if body.rotate_token:
        token = db.rotate_mcp_token()
    out = await get_settings()
    if token:
        out["mcp_token"] = token
        out["note"] = "Copia il token ora: verrà mostrato solo questa volta."
    return out


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
    return {"id": wid, "ok": True}


@app.post("/api/wallets/btc")
async def add_btc_wallet(body: BtcIn) -> dict[str, Any]:
    try:
        owner = normalize_owner(body.owner)
        kind = classify_btc_key(body.descriptor)
        # Prove we can derive at least one address before saving.
        bitcoin.derive_addresses(body.descriptor, gap=1)
    except Exception as exc:
        raise _http_secret(exc) from exc
    role = (body.role or "watch").strip().lower()
    if role not in {"watch", "cold", "freddo", "spesa", "hot", "multisig"}:
        role = "watch"
    try:
        wid = db.add_btc(
            body.label.strip(),
            kind,
            body.descriptor.strip(),
            owner,
            role=role,
            alert_out_btc=body.alert_out_btc,
            alert_min_btc=body.alert_min_btc,
            hot_threshold_btc=body.hot_threshold_btc,
        )
    except Exception as exc:
        raise HTTPException(status_code=409, detail=f"Non aggiunto: {exc}") from exc
    return {"id": wid, "ok": True, "kind": kind}


@app.delete("/api/wallets/evm/{wallet_id}")
async def remove_evm(wallet_id: int) -> dict[str, Any]:
    if not db.delete_evm(wallet_id):
        raise HTTPException(404, "Portafoglio non trovato")
    return {"ok": True}


@app.delete("/api/wallets/btc/{wallet_id}")
async def remove_btc(wallet_id: int) -> dict[str, Any]:
    if not db.delete_btc(wallet_id):
        raise HTTPException(404, "Portafoglio non trovato")
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
        "switches": data["switches"],
        "note": "Sono solo proposte. Nessuna transazione parte da qui.",
    }


@app.get("/api/alerts")
async def get_alerts(owner: str = Query("tutti")) -> dict[str, Any]:
    data = await snapshot(owner)
    return {"owner": owner, "protocol_security": data["security"], "btc_security": data["btc_security"]}


@app.get("/api/history")
async def get_history(owner: str = Query("tutti"), days: int = Query(90, ge=1, le=365)) -> dict[str, Any]:
    data = await snapshot(owner)
    cutoff = data["ts"] - days * 86400
    return {"owner": owner, "days": days, "history": [h for h in data["history"] if int(h.get("ts") or 0) >= cutoff]}


@app.get("/api/btc/wallets")
async def get_btc(owner: str = Query("tutti")) -> dict[str, Any]:
    data = await snapshot(owner)
    return {"owner": owner, "node": data["node"], "btc_price_usd": data["btc_price_usd"], "wallets": data["btc_wallets"], "moves": data["btc_moves"]}


@app.get("/api/btc/security")
async def get_btc_sec(owner: str = Query("tutti")) -> dict[str, Any]:
    data = await snapshot(owner)
    return {"owner": owner, "node": data["node"], "checks": data["btc_security"]}


@app.post("/api/refresh")
async def refresh_now() -> dict[str, Any]:
    data = await snapshot("tutti", force=True)
    return {"ok": True, "ts": data["ts"], "errors": data["errors"]}


if WEB_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")
