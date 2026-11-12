"""Read-only Ethereum RPC helpers and DeFi position scanner."""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

import httpx

from . import db
from .protocols import CHAINS, TOKENS, WETH

log = logging.getLogger("monitor.evm")
SELECTOR_BALANCE_OF = "70a08231"
SELECTOR_CONVERT_TO_ASSETS = "07a2d13a"


def _pad(addr: str) -> str:
    return addr.lower().replace("0x", "").zfill(64)


def _encode_balance_of(holder: str) -> str:
    return "0x" + SELECTOR_BALANCE_OF + _pad(holder)


def rpc_candidates(chain: str) -> list[str]:
    spec = CHAINS.get(chain)
    if spec is None:
        raise RuntimeError(f"Chain non configurata: {chain}")
    candidates: list[str] = []
    if chain == "ethereum":
        custom = (db.get_setting("eth_rpc_url") or "").strip()
        if custom:
            candidates.append(custom)
    for url in spec["rpcs"]:
        if url not in candidates:
            candidates.append(url)
    return candidates


def _parse_hex_int(value: Any) -> int:
    if value in (None, "0x", "0x0"):
        return 0
    if isinstance(value, int):
        return value
    text = str(value)
    if text.startswith("0x"):
        return int(text, 16)
    return int(text)


async def eth_rpc(method: str, params: list[Any], url: str | None = None, chain: str = "ethereum") -> Any:
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    last_error: Exception | None = None
    candidates = [url] if url else []
    for fallback in rpc_candidates(chain):
        if fallback not in candidates:
            candidates.append(fallback)
    label = CHAINS.get(chain, {}).get("label", chain)
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
                log.warning("RPC %s failed on %s (%s): %s", method, candidate, chain, exc)
    raise RuntimeError(f"Nessun RPC {label} disponibile: {last_error}")


async def eth_call_raw(to: str, data: str, chain: str = "ethereum") -> str:
    result = await eth_rpc("eth_call", [{"to": to, "data": data}, "latest"], chain=chain)
    return "" if result is None else str(result)


async def eth_call(to: str, data: str, chain: str = "ethereum") -> int:
    return _parse_hex_int(await eth_call_raw(to, data, chain))


async def eth_get_balance(address: str, chain: str = "ethereum") -> int:
    result = await eth_rpc("eth_getBalance", [address, "latest"], chain=chain)
    return _parse_hex_int(result)


async def gas_price_wei() -> int:
    result = await eth_rpc("eth_gasPrice", [], chain="ethereum")
    return _parse_hex_int(result)


async def token_balance(token: str, holder: str, chain: str = "ethereum") -> int:
    return await eth_call(token, _encode_balance_of(holder), chain)


async def convert_to_assets(token: str, shares: int, chain: str = "ethereum") -> int:
    """ERC-4626 convertToAssets(shares). Returns underlying base units."""
    if shares <= 0:
        return 0
    data = "0x" + SELECTOR_CONVERT_TO_ASSETS + shares.to_bytes(32, "big").hex()
    return await eth_call(token, data, chain)


def human(amount: int, decimals: int) -> float:
    if amount <= 0:
        return 0.0
    return float(Decimal(amount) / Decimal(10**decimals))


def price_key(chain: str, token: str) -> str:
    if not token:
        return ""
    if token.upper() == "ETH":
        return "ETH"
    return f"{chain}:{token}"


async def scan_wallet(
    address: str,
    owner: str,
    label: str,
    chains_ok: dict[str, bool] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Return positions and human-readable read failures.

    A failed balance or convertToAssets is logged and reported. It does not
    drop the other tokens, and a failed conversion still returns the position
    so the UI can show it without a dollar value.
    """
    import asyncio

    rows: list[dict[str, Any]] = []
    notes: list[str] = []
    ethereum_ok = True if chains_ok is None else bool(chains_ok.get("ethereum"))
    if ethereum_ok:
        try:
            wei = await eth_get_balance(address, "ethereum")
        except Exception as exc:  # noqa: BLE001
            msg = f"Lettura ETH nativo fallita: {exc}"
            log.warning(msg)
            notes.append(msg)
        else:
            eth = human(wei, 18)
            if eth > 0.0001:
                rows.append(
                    {
                        "owner": owner,
                        "label": label,
                        "address": address,
                        "chain": "ethereum",
                        "protocol": "Wallet",
                        "asset": "ETH",
                        "amount": eth,
                        "token": "ETH",
                        "price_token": price_key("ethereum", WETH),
                        "decimals": 18,
                        "llama_project": None,
                        "llama_symbol": None,
                        "valuation_ok": True,
                    }
                )

    async def _one(spec: dict[str, Any]) -> dict[str, Any] | None:
        chain = spec["chain"]
        if chains_ok is not None and not chains_ok.get(chain, False):
            return None
        try:
            raw = await token_balance(spec["token"], address, chain)
        except Exception as exc:  # noqa: BLE001
            msg = f"Lettura saldo fallita per {spec['protocol']} {spec['asset']} ({chain}): {exc}"
            log.warning(msg)
            notes.append(msg)
            return None
        if raw <= 0:
            return None
        amount = human(raw, int(spec["decimals"]))
        price_token = price_key(spec.get("price_chain") or chain, spec.get("price") or "")
        valuation_ok = True
        if spec["kind"] == "erc4626":
            try:
                assets = await convert_to_assets(spec["token"], raw, chain)
                amount = human(assets, int(spec["underlying_decimals"]))
                price_token = price_key(spec.get("price_chain") or chain, spec["underlying"])
            except Exception as exc:  # noqa: BLE001
                msg = (
                    f"convertToAssets fallito per {spec['protocol']} {spec['asset']} ({chain}): {exc}. "
                    "La posizione resta in elenco senza valore in dollari."
                )
                log.warning(msg)
                notes.append(msg)
                valuation_ok = False
                price_token = ""
        if amount <= 0:
            return None
        if valuation_ok and amount < 1e-8:
            return None
        return {
            "owner": owner,
            "label": label,
            "address": address,
            "chain": chain,
            "protocol": spec["protocol"],
            "asset": spec["asset"],
            "amount": amount,
            "token": spec["token"],
            "price_token": price_token,
            "decimals": spec["decimals"],
            "llama_project": spec.get("llama_project"),
            "llama_symbol": spec.get("llama_symbol"),
            "valuation_ok": valuation_ok,
        }

    found = await asyncio.gather(*[_one(spec) for spec in TOKENS])
    rows.extend([row for row in found if row])
    return rows, notes
