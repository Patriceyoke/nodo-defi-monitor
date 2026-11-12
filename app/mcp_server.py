"""Read-only MCP server (streamable HTTP). No signing, no transfers, no keys."""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from . import db
from .config import APP_VERSION
from .nft import collect_nfts
from .portfolio import snapshot
from .validation import normalize_owner

PROTOCOL_VERSION = "2025-03-26"
SERVER_INFO = {"name": "monitor-defi", "version": APP_VERSION}

TOOLS = [
    {
        "name": "get_portfolio",
        "description": "Panoramica del portafoglio (DeFi + Bitcoin) filtrata per titolare io/moglie/tutti.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string", "enum": ["io", "moglie", "tutti"], "default": "tutti"},
                "refresh": {"type": "boolean", "default": False},
            },
        },
    },
    {
        "name": "get_balances",
        "description": "Saldi e posizioni DeFi (e ETH in portafoglio) per titolare.",
        "inputSchema": {
            "type": "object",
            "properties": {"owner": {"type": "string", "enum": ["io", "moglie", "tutti"], "default": "tutti"}},
        },
    },
    {
        "name": "get_apys",
        "description": "APY attuale e media a 30 giorni delle posizioni, per titolare.",
        "inputSchema": {
            "type": "object",
            "properties": {"owner": {"type": "string", "enum": ["io", "moglie", "tutti"], "default": "tutti"}},
        },
    },
    {
        "name": "check_switch",
        "description": "Suggerisce uno switch solo se il rendimento sale a rischio uguale o inferiore, dopo il gas. Mai una transazione.",
        "inputSchema": {
            "type": "object",
            "properties": {"owner": {"type": "string", "enum": ["io", "moglie", "tutti"], "default": "tutti"}},
        },
    },
    {
        "name": "get_alerts",
        "description": "Avvisi di sicurezza sui protocolli in possesso e sui conti Bitcoin osservati.",
        "inputSchema": {
            "type": "object",
            "properties": {"owner": {"type": "string", "enum": ["io", "moglie", "tutti"], "default": "tutti"}},
        },
    },
    {
        "name": "get_history",
        "description": "Storico locale dei totali (SQLite), per titolare e periodo in giorni.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string", "enum": ["io", "moglie", "tutti"], "default": "tutti"},
                "days": {"type": "integer", "default": 90, "minimum": 1, "maximum": 365},
            },
        },
    },
    {
        "name": "get_btc_wallets",
        "description": "Conti Bitcoin in sola osservazione (saldo, ultimo movimento, stato dell'indicizzatore locale).",
        "inputSchema": {
            "type": "object",
            "properties": {"owner": {"type": "string", "enum": ["io", "moglie", "tutti"], "default": "tutti"}},
        },
    },
    {
        "name": "get_nfts",
        "description": "NFT in sola lettura dei portafogli salvati, con spam nascosto e un prezzo suggerito. Non mette in vendita e non firma.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string", "enum": ["io", "moglie", "tutti"], "default": "tutti"},
                "include_hidden": {"type": "boolean", "default": False},
            },
        },
    },
    {
        "name": "get_btc_security",
        "description": "Controlli di sicurezza Bitcoin in sola osservazione. Non chiede mai la seed.",
        "inputSchema": {
            "type": "object",
            "properties": {"owner": {"type": "string", "enum": ["io", "moglie", "tutti"], "default": "tutti"}},
        },
    },
]

FORBIDDEN_TOOLS = {
    "sign", "transfer", "send", "withdraw", "approve", "swap", "bridge",
    "broadcast", "sign_transaction", "send_transaction",
}

router = APIRouter()


def _authorized(authorization: str | None, x_token: str | None) -> None:
    expected = db.get_setting("mcp_token")
    provided = ""
    if authorization and authorization.lower().startswith("bearer "):
        provided = authorization.split(" ", 1)[1].strip()
    elif x_token:
        provided = x_token.strip()
    if not expected or provided != expected:
        raise HTTPException(status_code=401, detail="Token MCP mancante o non valido.")


def _rpc_result(id_: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _rpc_error(id_: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def _text(data: Any) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False, indent=2)}], "isError": False}


async def call_tool(name: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
    if name in FORBIDDEN_TOOLS or name.startswith("sign") or name.startswith("send"):
        return {
            "content": [{"type": "text", "text": "Questo strumento non esiste. Il server è in sola lettura e non firma né invia transazioni."}],
            "isError": True,
        }
    args = arguments or {}
    owner = normalize_owner(str(args.get("owner") or "tutti"), allow_all=True)
    if name == "get_portfolio":
        data = await snapshot(owner, force=bool(args.get("refresh")))
        return _text({k: data[k] for k in ("owner", "ts", "totals", "income", "positions", "btc_wallets", "switches", "errors", "node") if k in data})
    if name == "get_balances":
        data = await snapshot(owner)
        return _text({"owner": owner, "positions": data["positions"], "totals": data["totals"]})
    if name == "get_apys":
        data = await snapshot(owner)
        rows = [
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
        ]
        return _text({"owner": owner, "apys": rows})
    if name == "check_switch":
        data = await snapshot(owner)
        return _text(
            {
                "owner": owner,
                "gas_usd": data.get("gas_usd"),
                "switches": data["switches"],
                "note": "Sono solo proposte. Nessuna transazione parte da qui. La firma resta tua (es. OneKey).",
            }
        )
    if name == "get_alerts":
        data = await snapshot(owner)
        return _text({
            "owner": owner,
            "protocol_security": data["security"],
            "btc_security": data["btc_security"],
            "ens": data.get("ens_alerts") or [],
        })
    if name == "get_history":
        days = int(args.get("days") or 90)
        data = await snapshot(owner)
        hist = [h for h in data["history"] if True]
        # already filtered; trim by days in snapshot default 90
        cutoff = data["ts"] - days * 86400
        hist = [h for h in data["history"] if int(h.get("ts") or 0) >= cutoff]
        return _text({"owner": owner, "days": days, "history": hist})
    if name == "get_btc_wallets":
        data = await snapshot(owner)
        return _text({"owner": owner, "node": data["node"], "btc_price_usd": data["btc_price_usd"], "wallets": data["btc_wallets"], "moves": data["btc_moves"]})
    if name == "get_nfts":
        data = await collect_nfts(owner)
        include_hidden = bool(args.get("include_hidden"))
        collectibles = [item for item in data["items"] if not item.get("liquidity")]
        visible = collectibles if include_hidden else [item for item in collectibles if not item.get("hidden")]
        liquidity = [
            item for item in data["items"]
            if item.get("liquidity") and (include_hidden or not item.get("user_hidden"))
        ]
        return _text(
            {
                "owner": owner,
                "readonly": True,
                "source": data.get("source"),
                "limit_note": data.get("limit_note"),
                "listing_note": data.get("listing_note"),
                "errors": data.get("errors"),
                "nfts": [
                    {
                        "owner": item.get("owner"),
                        "chain": item.get("chain"),
                        "collection": item.get("collection"),
                        "name": item.get("name"),
                        "token_id": item.get("token_id"),
                        "hidden": item.get("hidden"),
                        "hidden_reason": item.get("hidden_reason"),
                        "floor_usd": item.get("floor_usd"),
                        "recent_sale_usd": item.get("recent_sale_usd"),
                        "listed": item.get("listed"),
                        "list_price_usd": item.get("list_price_usd"),
                        "suggestion_usd": item.get("suggestion_usd"),
                        "suggestion_reason": item.get("suggestion_reason"),
                    }
                    for item in visible
                ],
                "liquidity_positions": [
                    {
                        "owner": item.get("owner"),
                        "chain": item.get("chain"),
                        "collection": item.get("collection"),
                        "name": item.get("name"),
                        "token_id": item.get("token_id"),
                        "note": "Posizione di liquidità, non un NFT da vendere.",
                    }
                    for item in liquidity
                ],
            }
        )
    if name == "get_btc_security":
        data = await snapshot(owner)
        return _text({"owner": owner, "node": data["node"], "checks": data["btc_security"]})
    return {
        "content": [{"type": "text", "text": f"Strumento sconosciuto: {name}"}],
        "isError": True,
    }


async def handle_rpc(message: dict[str, Any]) -> dict[str, Any] | None:
    method = message.get("method")
    id_ = message.get("id")
    params = message.get("params") or {}
    if method == "initialize":
        return _rpc_result(
            id_,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_INFO,
                "instructions": (
                    "App in sola lettura. Non ha e non deve mai ricevere seed o chiavi private. "
                    "I tool accettano owner=io|moglie|tutti. Non esistono tool di firma o trasferimento."
                ),
            },
        )
    if method == "notifications/initialized" or method == "initialized" or (isinstance(method, str) and method.startswith("notifications/")):
        return None if id_ is None else _rpc_result(id_, {})
    if method == "ping":
        return _rpc_result(id_, {})
    if method == "tools/list":
        return _rpc_result(id_, {"tools": TOOLS})
    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        result = await call_tool(name, arguments)
        return _rpc_result(id_, result)
    if method == "resources/list":
        return _rpc_result(id_, {"resources": []})
    if method == "prompts/list":
        return _rpc_result(id_, {"prompts": []})
    return _rpc_error(id_, -32601, f"Metodo non supportato: {method}")


@router.api_route("/mcp", methods=["GET", "POST", "OPTIONS"])
@router.api_route("/mcp/", methods=["GET", "POST", "OPTIONS"])
async def mcp_endpoint(
    request: Request,
    authorization: str | None = Header(default=None),
    x_mcp_token: str | None = Header(default=None, alias="X-MCP-Token"),
) -> Response:
    if request.method == "OPTIONS":
        return Response(status_code=204)
    # GET is used by some clients to open an SSE stream; we accept it as a readiness probe.
    if request.method == "GET":
        _authorized(authorization, x_mcp_token)
        return PlainTextResponse("monitor-defi MCP ready\n")
    _authorized(authorization, x_mcp_token)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="JSON non valido")
    messages = body if isinstance(body, list) else [body]
    replies: list[dict[str, Any]] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        reply = await handle_rpc(msg)
        if reply is not None:
            replies.append(reply)
    if isinstance(body, list):
        return JSONResponse(replies)
    if not replies:
        return Response(status_code=202)
    return JSONResponse(replies[0])
