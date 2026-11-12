"""Read-only Ethereum RPC helpers and DeFi position scanner."""
from __future__ import annotations

import logging
import time
from decimal import Decimal
from typing import Any

import httpx

from . import db
from .protocols import CHAINS, TOKENS, WETH

log = logging.getLogger("monitor.evm")
SELECTOR_BALANCE_OF = "70a08231"
SELECTOR_CONVERT_TO_ASSETS = "07a2d13a"
SELECTOR_AGGREGATE3 = "82ad56cb"
SELECTOR_TOKEN_OF_OWNER = "2f745c59"
MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11"
USE_MULTICALL = True
# Last RPC that answered, per chain. Read by /api/debug/refresh-status.
_RPC_USED: dict[str, dict[str, Any]] = {}


def rpc_status() -> dict[str, dict[str, Any]]:
    return {chain: dict(info) for chain, info in _RPC_USED.items()}


def _remember_rpc(chain: str, url: str | None, ok: bool, error: str | None) -> None:
    _RPC_USED[chain] = {"url": url, "ok": ok, "error": error, "at": time.time()}


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
    # Keep each attempt short so Umbrel's proxy does not time out the whole refresh.
    async with httpx.AsyncClient(timeout=8.0) as client:
        for candidate in candidates:
            try:
                resp = await client.post(candidate, json=payload)
                resp.raise_for_status()
                data = resp.json()
                if "error" in data:
                    last_error = RuntimeError(str(data["error"]))
                    continue
                if method == "eth_blockNumber":
                    _remember_rpc(chain, candidate, True, None)
                return data.get("result")
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                log.warning("RPC %s failed on %s (%s): %s", method, candidate, chain, exc)
    if method == "eth_blockNumber":
        _remember_rpc(chain, None, False, str(last_error))
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


def encode_aggregate3(calls: list[tuple[str, str]]) -> str:
    """ABI-encode Multicall3.aggregate3. calls are (target, calldata hex)."""
    blobs: list[bytes] = []
    for target, calldata in calls:
        raw = bytes.fromhex(calldata[2:] if calldata.startswith("0x") else calldata)
        pad = (32 - len(raw) % 32) % 32
        blob = bytes.fromhex(_pad(target))
        blob += (1).to_bytes(32, "big")
        blob += (96).to_bytes(32, "big")
        blob += len(raw).to_bytes(32, "big")
        blob += raw + b"\x00" * pad
        blobs.append(blob)
    cursor = len(blobs) * 32
    offsets = []
    for blob in blobs:
        offsets.append(cursor)
        cursor += len(blob)
    body = len(blobs).to_bytes(32, "big")
    body += b"".join(item.to_bytes(32, "big") for item in offsets)
    body += b"".join(blobs)
    return "0x" + SELECTOR_AGGREGATE3 + (32).to_bytes(32, "big").hex() + body.hex()


def decode_aggregate3(result: str) -> list[tuple[bool, bytes]]:
    if not result or result == "0x":
        raise ValueError("multicall vuoto")
    raw = bytes.fromhex(result[2:])
    if len(raw) < 64:
        raise ValueError("multicall troppo corto")
    offset = int.from_bytes(raw[0:32], "big")
    length = int.from_bytes(raw[offset:offset + 32], "big")
    base = offset + 32
    out: list[tuple[bool, bytes]] = []
    for index in range(length):
        rel = int.from_bytes(raw[base + index * 32:base + (index + 1) * 32], "big")
        start = base + rel
        success = int.from_bytes(raw[start:start + 32], "big") == 1
        data_rel = int.from_bytes(raw[start + 32:start + 64], "big")
        data_at = start + data_rel
        size = int.from_bytes(raw[data_at:data_at + 32], "big")
        chunk = raw[data_at + 32:data_at + 32 + size]
        out.append((success, chunk))
    return out


async def multicall(chain: str, calls: list[tuple[str, str]]) -> list[tuple[bool, bytes]]:
    if not calls:
        return []
    raw = await eth_call_raw(MULTICALL3, encode_aggregate3(calls), chain)
    decoded = decode_aggregate3(raw)
    if len(decoded) != len(calls):
        raise ValueError("multicall incompleto")
    return decoded


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
) -> tuple[list[dict[str, Any]], list[str], list[dict[str, str]]]:
    """Return positions and human-readable read failures.

    A failed balance or convertToAssets is logged and reported. It does not
    drop the other tokens, and a failed conversion still returns the position
    so the UI can show it without a dollar value.
    """
    import asyncio

    rows: list[dict[str, Any]] = []
    notes: list[str] = []
    reports: list[dict[str, str]] = []
    ethereum_ok = True if chains_ok is None else bool(chains_ok.get("ethereum"))
    if ethereum_ok:
        try:
            wei = await eth_get_balance(address, "ethereum")
        except Exception as exc:  # noqa: BLE001
            msg = f"Lettura ETH nativo fallita: {exc}"
            log.warning(msg)
            notes.append(msg)
            reports.append({"chain": "ethereum", "asset": "ETH", "error": str(exc)})
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

    grouped: dict[str, list[dict[str, Any]]] = {}
    for spec in TOKENS:
        chain = spec["chain"]
        if chains_ok is not None and not chains_ok.get(chain, False):
            continue
        grouped.setdefault(chain, []).append(spec)

    balances: dict[tuple[str, str], int] = {}
    for chain, specs in grouped.items():
        try:
            if not USE_MULTICALL:
                raise RuntimeError("multicall disattivato")
            calls = [(spec["token"], _encode_balance_of(address)) for spec in specs]
            decoded = await multicall(chain, calls)
            for spec, (ok, chunk) in zip(specs, decoded):
                if not ok or len(chunk) < 32:
                    raise RuntimeError(f"saldo {spec['asset']} non letto")
                balances[(chain, spec["token"])] = int.from_bytes(chunk[:32], "big")
        except Exception as exc:  # noqa: BLE001
            log.info("Multicall %s non usato (%s), lettura singola.", chain, exc)
            async def _bal(spec: dict[str, Any], chain: str = chain) -> None:
                try:
                    balances[(chain, spec["token"])] = await token_balance(spec["token"], address, chain)
                except Exception as bal_exc:  # noqa: BLE001
                    msg = f"Lettura saldo fallita per {spec['protocol']} {spec['asset']} ({chain}): {bal_exc}"
                    log.warning(msg)
                    notes.append(msg)
                    reports.append({"chain": chain, "asset": f"{spec['protocol']} {spec['asset']}", "error": str(bal_exc)})
            await asyncio.gather(*[_bal(spec) for spec in specs])

    vault_assets: dict[tuple[str, str], int] = {}
    for chain, specs in grouped.items():
        vaults = [
            spec for spec in specs
            if spec["kind"] == "erc4626" and balances.get((chain, spec["token"]), 0) > 0
        ]
        if not vaults or not USE_MULTICALL:
            continue
        try:
            calls = [
                (spec["token"], "0x" + SELECTOR_CONVERT_TO_ASSETS + balances[(chain, spec["token"])].to_bytes(32, "big").hex())
                for spec in vaults
            ]
            decoded = await multicall(chain, calls)
            for spec, (ok, chunk) in zip(vaults, decoded):
                if ok and len(chunk) >= 32:
                    vault_assets[(chain, spec["token"])] = int.from_bytes(chunk[:32], "big")
        except Exception as exc:  # noqa: BLE001
            log.info("Multicall convertToAssets %s non usato (%s).", chain, exc)

    async def _one(spec: dict[str, Any]) -> dict[str, Any] | None:
        chain = spec["chain"]
        if chains_ok is not None and not chains_ok.get(chain, False):
            return None
        key = (chain, spec["token"])
        if key not in balances:
            return None
        raw = balances[key]
        if raw <= 0:
            return None
        amount = human(raw, int(spec["decimals"]))
        price_token = price_key(spec.get("price_chain") or chain, spec.get("price") or "")
        valuation_ok = True
        if spec["kind"] == "erc4626":
            try:
                assets = vault_assets.get(key)
                if assets is None:
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
    return rows, notes, reports
