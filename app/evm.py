"""Read-only Ethereum RPC helpers and DeFi position scanner."""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

import httpx

from . import db
from .config import DEFAULT_RPC, PUBLIC_RPCS
from .protocols import TOKENS, WETH

log = logging.getLogger("monitor.evm")
SELECTOR_BALANCE_OF = "70a08231"
SELECTOR_DECIMALS = "313ce567"
SELECTOR_TOTAL_SUPPLY = "18160ddd"
# Aave v3 Pool.getReserveData(address) is large; we use utilization via DefiLlama instead.


def _pad(addr: str) -> str:
    return addr.lower().replace("0x", "").zfill(64)


def _encode_balance_of(holder: str) -> str:
    return "0x" + SELECTOR_BALANCE_OF + _pad(holder)


def rpc_url() -> str:
    custom = (db.get_setting("eth_rpc_url") or "").strip()
    return custom or DEFAULT_RPC


def _parse_hex_int(value: Any) -> int:
    if value in (None, "0x", "0x0"):
        return 0
    if isinstance(value, int):
        return value
    text = str(value)
    if text.startswith("0x"):
        return int(text, 16)
    return int(text)


async def eth_rpc(method: str, params: list[Any], url: str | None = None) -> Any:
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    last_error: Exception | None = None
    candidates = [url or rpc_url()]
    for fallback in PUBLIC_RPCS:
        if fallback not in candidates:
            candidates.append(fallback)
    async with httpx.AsyncClient(timeout=20.0) as client:
        for candidate in candidates:
            try:
                resp = await client.post(candidate, json=payload)
                resp.raise_for_status()
                data = resp.json()
                if "error" in data:
                    last_error = RuntimeError(str(data["error"]))
                    continue
                return data.get("result")
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                log.warning("RPC %s failed on %s: %s", method, candidate, exc)
    raise RuntimeError(f"Nessun RPC Ethereum disponibile: {last_error}")


async def eth_call(to: str, data: str) -> int:
    result = await eth_rpc("eth_call", [{"to": to, "data": data}, "latest"])
    return _parse_hex_int(result)


async def eth_get_balance(address: str) -> int:
    result = await eth_rpc("eth_getBalance", [address, "latest"])
    return _parse_hex_int(result)


async def gas_price_wei() -> int:
    result = await eth_rpc("eth_gasPrice", [])
    return _parse_hex_int(result)


async def token_balance(token: str, holder: str) -> int:
    return await eth_call(token, _encode_balance_of(holder))


def human(amount: int, decimals: int) -> float:
    if amount <= 0:
        return 0.0
    return float(Decimal(amount) / Decimal(10**decimals))


async def scan_wallet(address: str, owner: str, label: str) -> list[dict[str, Any]]:
    """Return token and ETH balances that are above a tiny dust threshold."""
    import asyncio

    rows: list[dict[str, Any]] = []
    wei = await eth_get_balance(address)
    eth = human(wei, 18)
    if eth > 0.0001:
        rows.append(
            {
                "owner": owner,
                "label": label,
                "address": address,
                "protocol": "Wallet",
                "asset": "ETH",
                "amount": eth,
                "token": "ETH",
                "price_token": WETH,
                "decimals": 18,
                "llama_project": None,
                "llama_symbol": "ETH",
            }
        )

    async def _one(spec: dict[str, Any]) -> dict[str, Any] | None:
        try:
            raw = await token_balance(spec["token"], address)
        except Exception as exc:  # noqa: BLE001
            log.debug("balanceOf failed for %s %s: %s", spec["protocol"], spec["asset"], exc)
            return None
        amount = human(raw, int(spec["decimals"]))
        if amount <= 0:
            return None
        if spec["asset"] in {"USDC", "USDT", "DAI", "USDS"} and amount < 0.05:
            return None
        if amount < 1e-8:
            return None
        return {
            "owner": owner,
            "label": label,
            "address": address,
            "protocol": spec["protocol"],
            "asset": spec["asset"],
            "amount": amount,
            "token": spec["token"],
            "price_token": spec["price"],
            "decimals": spec["decimals"],
            "llama_project": spec.get("llama_project"),
            "llama_symbol": spec.get("llama_symbol"),
        }

    found = await asyncio.gather(*[_one(spec) for spec in TOKENS])
    rows.extend([row for row in found if row])
    return rows
