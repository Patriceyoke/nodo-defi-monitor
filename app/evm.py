"""Read-only Ethereum RPC helpers and DeFi position scanner."""
from __future__ import annotations

import asyncio
import logging
import time
from decimal import Decimal
from typing import Any

import httpx

from . import db
from .protocols import (
    AAVE_V4_SPOKES,
    CHAINS,
    TOKENS,
    WETH,
    aave_v4_asset_symbol,
    aave_v4_hub_name,
)

log = logging.getLogger("monitor.evm")
SELECTOR_BALANCE_OF = "70a08231"
SELECTOR_CONVERT_TO_ASSETS = "07a2d13a"
SELECTOR_AAVE_V4_RESERVE_COUNT = "99806546"
SELECTOR_AAVE_V4_GET_RESERVE = "77778db3"
SELECTOR_AAVE_V4_USER_SUPPLIED = "f1568a89"
SELECTOR_AAVE_V4_USER_TOTAL_DEBT = "9b7172a6"
SELECTOR_SYMBOL = "95d89b41"
_V4_CATALOG: dict[str, Any] = {"at": 0.0, "rows": None}
_V4_SYMBOLS: dict[str, str] = {}
SELECTOR_AGGREGATE3 = "82ad56cb"
SELECTOR_TOKEN_OF_OWNER = "2f745c59"
MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11"
USE_MULTICALL = True
# Last RPC that answered, per chain. Read by /api/debug/refresh-status.
_RPC_USED: dict[str, dict[str, Any]] = {}
# Dead endpoints are skipped until this timestamp. A few minutes, then they are tried again.
_CIRCUIT: dict[str, float] = {}
_MULTICALL_SEEN: dict[str, tuple[bool, float]] = {}
CIRCUIT_SECONDS = 180
# Each Aave reader gets this long per wallet. Past it, the wallet keeps its other balances.
AAVE_READ_TIMEOUT = 25.0
RPC_BACKOFF_SECONDS = 0.15


def reset_rpc_circuit() -> None:
    _CIRCUIT.clear()
    _MULTICALL_SEEN.clear()


def _circuit_open(url: str) -> bool:
    return (_CIRCUIT.get(url) or 0) > time.time()


def _trip_circuit(url: str) -> None:
    _CIRCUIT[url] = time.time() + CIRCUIT_SECONDS


def _http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=8.0)


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
    live = [item for item in candidates if not _circuit_open(item)]
    if not live:
        live = list(candidates)
    # Keep each attempt short so Umbrel's proxy does not time out the whole refresh.
    async with _http_client() as client:
        for index, candidate in enumerate(live):
            try:
                resp = await client.post(candidate, json=payload)
                if resp.status_code == 429:
                    last_error = RuntimeError("HTTP 429")
                    _trip_circuit(candidate)
                    log.warning("RPC %s ha risposto 429 su %s (%s)", method, candidate, chain)
                    if index < len(live) - 1:
                        await asyncio.sleep(RPC_BACKOFF_SECONDS)
                    continue
                resp.raise_for_status()
                data = resp.json()
                if "error" in data:
                    last_error = RuntimeError(str(data["error"]))
                    continue
                if method == "eth_blockNumber":
                    _remember_rpc(chain, candidate, True, None)
                return data.get("result")
            except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError) as exc:
                last_error = exc
                _trip_circuit(candidate)
                log.warning("RPC %s non raggiungibile su %s (%s): %s", method, candidate, chain, exc)
                if index < len(live) - 1:
                    await asyncio.sleep(RPC_BACKOFF_SECONDS)
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None and exc.response.status_code >= 500:
                    _trip_circuit(candidate)
                log.warning("RPC %s failed on %s (%s): %s", method, candidate, chain, exc)
                if index < len(live) - 1:
                    await asyncio.sleep(RPC_BACKOFF_SECONDS)
    if method == "eth_blockNumber":
        _remember_rpc(chain, None, False, str(last_error))
    raise RuntimeError(f"Nessun RPC {label} disponibile: {last_error}")


async def eth_call_raw(to: str, data: str, chain: str = "ethereum", block: str | None = None) -> str:
    tag = block or "latest"
    result = await eth_rpc("eth_call", [{"to": to, "data": data}, tag], chain=chain)
    return "" if result is None else str(result)


async def eth_call(to: str, data: str, chain: str = "ethereum", block: str | None = None) -> int:
    return _parse_hex_int(await eth_call_raw(to, data, chain, block=block))


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


def apply_multicall_reads(
    specs: list[dict[str, Any]],
    decoded: list[tuple[bool, bytes]],
) -> tuple[dict[tuple[str, str], int], list[dict[str, Any]]]:
    """Keep every successful sub-call. A failed one is returned on its own."""
    balances: dict[tuple[str, str], int] = {}
    failed: list[dict[str, Any]] = []
    for spec, item in zip(specs, decoded):
        ok, chunk = item
        if ok and len(chunk) >= 32:
            balances[(spec["chain"], spec["token"])] = int.from_bytes(chunk[:32], "big")
        else:
            failed.append(spec)
    return balances, failed


async def multicall_available(chain: str) -> bool:
    """False when Multicall3 has no code on this chain. Cached for ten minutes."""
    seen = _MULTICALL_SEEN.get(chain)
    if seen and time.time() - seen[1] < 600:
        return seen[0]
    try:
        code = await eth_rpc("eth_getCode", [MULTICALL3, "latest"], chain=chain)
        ok = isinstance(code, str) and code not in {"0x", "0x0"} and len(code) > 2
    except Exception as exc:  # noqa: BLE001
        log.info("Multicall3 non verificato su %s: %s", chain, exc)
        ok = False
    _MULTICALL_SEEN[chain] = (ok, time.time())
    return ok


async def multicall(chain: str, calls: list[tuple[str, str]], block: str | None = None) -> list[tuple[bool, bytes]]:
    if not calls:
        return []
    tag = block
    if not tag:
        try:
            tag = await eth_rpc("eth_blockNumber", [], chain=chain)
        except Exception:  # noqa: BLE001
            tag = "latest"
    raw = await eth_call_raw(MULTICALL3, encode_aggregate3(calls), chain, block=str(tag or "latest"))
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
    chain_blocks: dict[str, str] = {}
    for chain, specs in grouped.items():
        try:
            if not USE_MULTICALL:
                raise RuntimeError("multicall disattivato")
            if not await multicall_available(chain):
                raise RuntimeError("Multicall3 assente su questa rete")
            try:
                block = str(await eth_rpc("eth_blockNumber", [], chain=chain) or "latest")
            except Exception:  # noqa: BLE001
                block = "latest"
            chain_blocks[chain] = block
            calls = [(spec["token"], _encode_balance_of(address)) for spec in specs]
            decoded = await multicall(chain, calls, block=block)
            got, failed = apply_multicall_reads(specs, decoded)
            balances.update(got)
            for spec in failed:
                try:
                    balances[(chain, spec["token"])] = await eth_call(
                        spec["token"], _encode_balance_of(address), chain, block=block
                    )
                except Exception as bal_exc:  # noqa: BLE001
                    msg = f"Lettura saldo fallita per {spec['protocol']} {spec['asset']} ({chain}): {bal_exc}"
                    log.warning(msg)
                    notes.append(msg)
                    reports.append({
                        "chain": chain,
                        "asset": f"{spec['protocol']} {spec['asset']}",
                        "error": str(bal_exc),
                    })
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
            decoded = await multicall(chain, calls, block=chain_blocks.get(chain))
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
    if ethereum_ok:
        static_tokens = {str(spec["token"]).lower() for spec in TOKENS if spec["chain"] == "ethereum"}
        try:
            v3_rows, v3_notes, v3_reports, locked = await asyncio.wait_for(
                scan_aave_v3(address, owner, label, static_tokens), timeout=AAVE_READ_TIMEOUT
            )
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, asyncio.TimeoutError):
                exc = RuntimeError(f"nessuna risposta entro {AAVE_READ_TIMEOUT:.0f} s")
            msg = f"Lettura Aave V3 di {label} fallita: {exc}. Prestiti e garanzie non contati in questo giro."
            log.warning(msg)
            notes.append(msg)
            reports.append({"chain": "ethereum", "asset": "Aave V3", "error": str(exc)})
        else:
            for row in rows:
                if str(row.get("token") or "").lower() in locked:
                    row["collateral"] = True
                    row["collateral_locked"] = True
            rows.extend(v3_rows)
            notes.extend(v3_notes)
            reports.extend(v3_reports)
        try:
            v4_rows, v4_notes, v4_reports = await asyncio.wait_for(
                scan_aave_v4(address, owner, label), timeout=AAVE_READ_TIMEOUT
            )
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, asyncio.TimeoutError):
                exc = RuntimeError(f"nessuna risposta entro {AAVE_READ_TIMEOUT:.0f} s")
            msg = f"Lettura Aave V4 fallita: {exc}"
            log.warning(msg)
            notes.append(msg)
            reports.append({"chain": "ethereum", "asset": "Aave V4", "error": str(exc)})
        else:
            rows.extend(v4_rows)
            notes.extend(v4_notes)
            reports.extend(v4_reports)
    return rows, notes, reports


def decode_aave_v4_reserve(data: bytes | str) -> tuple[str, str, int]:
    """getReserve words: [0] underlying, [1] hub, [3] decimals."""
    if isinstance(data, str):
        text = data[2:] if data.startswith("0x") else data
        raw = bytes.fromhex(text) if text else b""
    else:
        raw = data
    words = [raw[i:i + 32] for i in range(0, len(raw) - 31, 32)]
    if not words:
        raise ValueError("riserva Aave V4 vuota")
    if int.from_bytes(words[0], "big") == 32 and len(words) >= 5:
        words = words[1:]
    if len(words) < 4:
        raise ValueError("riserva Aave V4 incompleta")
    underlying = "0x" + words[0][-20:].hex()
    hub = "0x" + words[1][-20:].hex()
    decimals = int.from_bytes(words[3], "big")
    return underlying, hub, decimals


def _encode_word(value: int) -> str:
    return value.to_bytes(32, "big").hex()


async def _each_call(calls: list[tuple[str, str]]) -> list[tuple[bool, bytes]]:
    out: list[tuple[bool, bytes]] = []
    for target, data in calls:
        try:
            raw = await eth_call_raw(target, data, "ethereum")
            text = raw[2:] if raw.startswith("0x") else raw
            out.append((True, bytes.fromhex(text) if text else b""))
        except Exception:  # noqa: BLE001
            out.append((False, b""))
    return out


MULTICALL_CHUNK = 60
SINGLE_CALL_LIMIT = 12


async def _multicall_or_each(calls: list[tuple[str, str]]) -> list[tuple[bool, bytes]]:
    """Read in chunks of MULTICALL_CHUNK. A long list never falls back to one call each.

    A public RPC that rejects a big aggregate3 or answers 429 must not turn one
    wallet into hundreds of single calls: that is what kept a refresh running for
    minutes. Short lists may still go one by one.
    """
    if not calls:
        return []
    try:
        if not USE_MULTICALL:
            raise RuntimeError("multicall disattivato")
        out: list[tuple[bool, bytes]] = []
        for start in range(0, len(calls), MULTICALL_CHUNK):
            out.extend(await multicall("ethereum", calls[start:start + MULTICALL_CHUNK], block="latest"))
        return out
    except Exception as exc:  # noqa: BLE001
        if len(calls) > SINGLE_CALL_LIMIT:
            raise RuntimeError(f"lettura multipla non riuscita ({exc})") from exc
        log.info("Multicall non usato (%s), lettura singola.", exc)
        return await _each_call(calls)


def _decode_symbol(data: str) -> str:
    text = data[2:] if data.startswith("0x") else data
    raw = bytes.fromhex(text) if text else b""
    if len(raw) >= 64:
        offset = int.from_bytes(raw[0:32], "big")
        if offset + 32 <= len(raw):
            size = int.from_bytes(raw[offset:offset + 32], "big")
            chunk = raw[offset + 32:offset + 32 + size]
            decoded = chunk.decode("utf-8", "ignore").strip("\x00 ").strip()
            if decoded:
                return decoded
    return raw.rstrip(b"\x00").decode("utf-8", "ignore").strip()


async def _underlying_symbol(underlying: str) -> str:
    known = aave_v4_asset_symbol(underlying)
    if known:
        return known
    cached = _V4_SYMBOLS.get(underlying.lower())
    if cached:
        return cached
    try:
        raw = await eth_call_raw(underlying, "0x" + SELECTOR_SYMBOL, "ethereum")
        symbol = _decode_symbol(raw) or "TOKEN"
    except Exception:  # noqa: BLE001
        symbol = "TOKEN"
    _V4_SYMBOLS[underlying.lower()] = symbol
    return symbol


_V4_LOCK = asyncio.Lock()


async def aave_v4_catalog() -> list[dict[str, Any]]:
    """Spoke reserves, cached for one hour. One build is shared by every wallet."""
    async with _V4_LOCK:
        return await _aave_v4_catalog()


async def _aave_v4_catalog() -> list[dict[str, Any]]:
    now = time.time()
    cached = _V4_CATALOG.get("rows")
    if cached is not None and now - float(_V4_CATALOG.get("at") or 0) < 3600:
        return list(cached)
    found: list[dict[str, Any]] = []
    for spoke in AAVE_V4_SPOKES:
        try:
            count = await eth_call(spoke["address"], "0x" + SELECTOR_AAVE_V4_RESERVE_COUNT, "ethereum")
        except Exception as exc:  # noqa: BLE001
            log.warning("Aave V4 %s non letto: %s", spoke["name"], exc)
            continue
        if count <= 0:
            continue
        calls = [
            (spoke["address"], "0x" + SELECTOR_AAVE_V4_GET_RESERVE + _encode_word(reserve_id))
            for reserve_id in range(count)
        ]
        decoded = await _multicall_or_each(calls)
        for reserve_id, (ok, chunk) in zip(range(count), decoded):
            if not ok or not chunk:
                continue
            try:
                underlying, hub, decimals = decode_aave_v4_reserve(chunk)
            except Exception as exc:  # noqa: BLE001
                log.info("Riserva Aave V4 %s #%s scartata: %s", spoke["name"], reserve_id, exc)
                continue
            found.append({
                "spoke": spoke["address"],
                "spoke_name": spoke["name"],
                "reserve_id": reserve_id,
                "underlying": underlying,
                "hub": hub,
                "decimals": decimals,
            })
    if found:
        _V4_CATALOG["at"] = now
        _V4_CATALOG["rows"] = found
    return found


async def scan_aave_v4(
    address: str,
    owner: str,
    label: str,
) -> tuple[list[dict[str, Any]], list[str], list[dict[str, str]]]:
    """Supplied underlying on Aave V4 spokes. These balances are not ERC-20."""
    notes: list[str] = []
    reports: list[dict[str, str]] = []
    try:
        catalog = await aave_v4_catalog()
    except Exception as exc:  # noqa: BLE001
        msg = f"Lettura catalogo Aave V4 fallita: {exc}"
        return [], [msg], [{"chain": "ethereum", "asset": "Aave V4", "error": str(exc)}]
    calls = [
        (
            item["spoke"],
            "0x" + SELECTOR_AAVE_V4_USER_SUPPLIED + _encode_word(int(item["reserve_id"])) + _pad(address),
        )
        for item in catalog
    ] + [
        (
            item["spoke"],
            "0x" + SELECTOR_AAVE_V4_USER_TOTAL_DEBT + _encode_word(int(item["reserve_id"])) + _pad(address),
        )
        for item in catalog
    ]
    decoded = await _multicall_or_each(calls)
    rows: list[dict[str, Any]] = []
    entries = [(item, False) for item in catalog] + [(item, True) for item in catalog]
    for (item, is_debt), (ok, chunk) in zip(entries, decoded):
        if not ok or len(chunk) < 32:
            continue
        raw_amount = int.from_bytes(chunk[:32], "big")
        if raw_amount <= 0:
            continue
        try:
            symbol = await _underlying_symbol(item["underlying"])
        except Exception as exc:  # noqa: BLE001
            notes.append(f"Simbolo Aave V4 non letto: {exc}")
            reports.append({"chain": "ethereum", "asset": "Aave V4", "error": str(exc)})
            symbol = "TOKEN"
        hub_name = aave_v4_hub_name(item["hub"]) or item["spoke_name"]
        amount = human(raw_amount, int(item["decimals"]))
        if amount <= 0:
            continue
        if is_debt:
            # Borrow rate is not read for V4 yet: the debt lowers the total, income shows n/d.
            rows.append({
                "owner": owner,
                "label": label,
                "address": address,
                "chain": "ethereum",
                "protocol": "Debito Aave V4",
                "asset": symbol,
                "amount": amount,
                "token": item["underlying"],
                "price_token": price_key("ethereum", item["underlying"]),
                "decimals": int(item["decimals"]),
                "llama_project": None,
                "llama_symbol": None,
                "market": hub_name,
                "valuation_ok": True,
                "debt": True,
                "debt_kind": "variabile",
                "borrow_apy": None,
                "debt_asset": symbol,
            })
            continue
        rows.append({
            "owner": owner,
            "label": label,
            "address": address,
            "chain": "ethereum",
            "protocol": "Aave V4",
            "asset": symbol,
            "amount": amount,
            "token": item["underlying"],
            "price_token": price_key("ethereum", item["underlying"]),
            "decimals": int(item["decimals"]),
            "llama_project": "aave-v4",
            "llama_symbol": symbol,
            "llama_meta": hub_name,
            "market": hub_name,
            "valuation_ok": True,
        })
    return rows, notes, reports


# --- Aave V3-style pools: supplies (aTokens) and debt (variable/stable debt tokens) ---

SELECTOR_V3_RESERVES_LIST = "d1946dbc"
SELECTOR_V3_RESERVE_DATA = "35ea6a75"
SELECTOR_V3_ACCOUNT_DATA = "bf92857c"
SELECTOR_V3_USER_CONFIG = "4417a583"
SECONDS_PER_YEAR = 31_536_000
RAY = 10**27
V3_CATALOG_SECONDS = 300
_V3_CATALOG: dict[str, dict[str, Any]] = {}
_V3_LOCK = asyncio.Lock()


def ray_rate_to_apy_pct(rate_ray: int) -> float:
    """Aave rates are yearly APR in ray. Compounded per second, as the Aave app shows them."""
    apr = Decimal(int(rate_ray)) / Decimal(RAY)
    if apr <= 0:
        return 0.0
    return float(((1 + apr / SECONDS_PER_YEAR) ** SECONDS_PER_YEAR - 1) * 100)


def decode_v3_reserve_data(chunk: bytes) -> dict[str, Any]:
    """getReserveData (ReserveDataLegacy): 15 words. Same layout on Aave 3.0-3.4 and SparkLend."""
    words = [chunk[i:i + 32] for i in range(0, len(chunk) - 31, 32)]
    if len(words) < 11:
        raise ValueError("getReserveData incompleto")
    configuration = int.from_bytes(words[0], "big")
    stable = "0x" + words[9][-20:].hex()
    return {
        "decimals": (configuration >> 48) & 0xFF,
        "liquidity_rate": int.from_bytes(words[2], "big"),
        "variable_borrow_rate": int.from_bytes(words[4], "big"),
        "stable_borrow_rate": int.from_bytes(words[5], "big"),
        "id": int.from_bytes(words[7], "big"),
        "a_token": "0x" + words[8][-20:].hex(),
        "stable_debt": "" if int(stable, 16) == 0 else stable,
        "variable_debt": "0x" + words[10][-20:].hex(),
    }


def decode_account_data(chunk: bytes) -> dict[str, float | None]:
    """getUserAccountData: collateral, debt (base currency, 8 decimals), ..., healthFactor (1e18)."""
    words = [int.from_bytes(chunk[i:i + 32], "big") for i in range(0, len(chunk) - 31, 32)]
    if len(words) < 6:
        raise ValueError("getUserAccountData incompleto")
    debt = words[1] / 1e8
    return {
        "collateral_usd": words[0] / 1e8,
        "debt_usd": debt,
        "liquidation_threshold_pct": words[3] / 100.0,
        "health_factor": (words[5] / 1e18) if debt > 0 else None,
    }


async def _v3_pool_catalog(pool: dict[str, Any]) -> list[dict[str, Any]]:
    """Reserves of one pool with live rates. Shared by every wallet for a few minutes."""
    key = pool["address"].lower()
    async with _V3_LOCK:
        cached = _V3_CATALOG.get(key)
        if cached and time.time() - float(cached["at"]) < V3_CATALOG_SECONDS:
            return list(cached["rows"])
        raw = await eth_call_raw(pool["address"], "0x" + SELECTOR_V3_RESERVES_LIST, "ethereum")
        data = bytes.fromhex(raw[2:] if raw.startswith("0x") else raw)
        if len(data) < 64:
            raise ValueError("getReservesList vuoto")
        count = int.from_bytes(data[32:64], "big")
        underlyings = ["0x" + data[64 + i * 32 + 12:64 + (i + 1) * 32].hex() for i in range(count)]
        decoded = await _multicall_or_each(
            [(pool["address"], "0x" + SELECTOR_V3_RESERVE_DATA + _pad(u)) for u in underlyings]
        )
        rows: list[dict[str, Any]] = []
        for underlying, (ok, chunk) in zip(underlyings, decoded):
            if not ok or not chunk:
                continue
            try:
                item = decode_v3_reserve_data(chunk)
            except Exception as exc:  # noqa: BLE001
                log.info("Riserva %s scartata: %s", pool["name"], exc)
                continue
            item["underlying"] = underlying
            rows.append(item)
        _V3_CATALOG[key] = {"at": time.time(), "rows": rows}
        return list(rows)


def _collateral_on(config_bits: int, reserve_id: int) -> bool:
    return bool((config_bits >> (reserve_id * 2 + 1)) & 1)


async def scan_aave_v3(
    address: str,
    owner: str,
    label: str,
    skip_tokens: set[str] | None = None,
) -> tuple[list[dict[str, Any]], list[str], list[dict[str, str]], dict[str, dict[str, Any]]]:
    """Supplies and debts on Aave V3-style pools.

    Debt rows carry debt=True and are valued negative in portfolio._enrich.
    skip_tokens holds aTokens already read from the static TOKENS list, so a
    supply is never counted twice. The last return value maps those aTokens to
    their pool's account data when they back a loan.
    """
    from .protocols import AAVE_V3_POOLS, LIDO_UNDERLYINGS

    skip = {t.lower() for t in (skip_tokens or set())}
    rows: list[dict[str, Any]] = []
    notes: list[str] = []
    reports: list[dict[str, str]] = []
    locked: dict[str, dict[str, Any]] = {}
    probes = await _multicall_or_each(
        [(pool["address"], "0x" + SELECTOR_V3_ACCOUNT_DATA + _pad(address)) for pool in AAVE_V3_POOLS]
        + [(pool["address"], "0x" + SELECTOR_V3_USER_CONFIG + _pad(address)) for pool in AAVE_V3_POOLS]
    )
    n = len(AAVE_V3_POOLS)
    for index, pool in enumerate(AAVE_V3_POOLS):
        ok, chunk = probes[index]
        cfg_ok, cfg_chunk = probes[n + index]
        if not ok or len(chunk) < 192:
            msg = f"Lettura {pool['name']} fallita: conto non letto."
            notes.append(msg)
            reports.append({"chain": "ethereum", "asset": pool["name"], "error": "getUserAccountData"})
            continue
        account = decode_account_data(chunk)
        config_bits = int.from_bytes(cfg_chunk[:32], "big") if cfg_ok and len(cfg_chunk) >= 32 else 0
        # A supply with collateral switched off is not in collateral_usd, so the
        # aTokens are always read. Debt tokens only when the pool reports debt.
        try:
            catalog = await _v3_pool_catalog(pool)
        except Exception as exc:  # noqa: BLE001
            msg = f"Lettura riserve {pool['name']} fallita: {exc}"
            log.warning(msg)
            notes.append(msg)
            reports.append({"chain": "ethereum", "asset": pool["name"], "error": str(exc)})
            continue
        calls: list[tuple[str, str]] = []
        index_of: list[tuple[dict[str, Any], str]] = []
        for item in catalog:
            kinds = ("a_token", "variable_debt", "stable_debt") if account["debt_usd"] else ("a_token",)
            for kind in kinds:
                token = item.get(kind)
                if token:
                    calls.append((token, _encode_balance_of(address)))
                    index_of.append((item, kind))
        decoded = await _multicall_or_each(calls)
        debts: list[dict[str, Any]] = []
        collateral_assets: list[str] = []
        for (item, kind), (ok, chunk) in zip(index_of, decoded):
            if not ok or len(chunk) < 32:
                continue
            raw_amount = int.from_bytes(chunk[:32], "big")
            if raw_amount <= 0:
                continue
            amount = human(raw_amount, int(item["decimals"]))
            if amount < 1e-8:
                continue
            symbol = await _underlying_symbol(item["underlying"])
            base = {
                "owner": owner,
                "label": label,
                "address": address,
                "chain": "ethereum",
                "asset": symbol,
                "amount": amount,
                "price_token": price_key("ethereum", item["underlying"]),
                "decimals": int(item["decimals"]),
                "market": pool["name"],
                "valuation_ok": True,
            }
            if kind == "a_token":
                is_collateral = _collateral_on(config_bits, int(item["id"]))
                if is_collateral:
                    collateral_assets.append(symbol)
                if item["a_token"].lower() in skip:
                    if is_collateral and account["debt_usd"]:
                        locked[item["a_token"].lower()] = {"market": pool["name"], **account}
                    continue
                rows.append({
                    **base,
                    "protocol": pool["protocol"],
                    "token": item["a_token"],
                    "llama_project": pool["llama_project"],
                    "llama_symbol": symbol,
                    "onchain_apy": ray_rate_to_apy_pct(item["liquidity_rate"]),
                    "lido_base": symbol.lower() in LIDO_UNDERLYINGS,
                    "collateral": is_collateral,
                    "collateral_locked": bool(is_collateral and account["debt_usd"]),
                })
            else:
                rate = item["variable_borrow_rate"] if kind == "variable_debt" else item["stable_borrow_rate"]
                debts.append({
                    **base,
                    "protocol": pool["debt_protocol"],
                    "token": item[kind],
                    "llama_project": None,
                    "llama_symbol": None,
                    "debt": True,
                    "debt_kind": "variabile" if kind == "variable_debt" else "stabile",
                    "borrow_apy": ray_rate_to_apy_pct(rate),
                    "debt_asset": symbol,
                })
        for row in debts:
            row.update({
                "health_factor": account["health_factor"],
                "debt_usd": account["debt_usd"],
                "collateral_usd": account["collateral_usd"],
                "liquidation_threshold_pct": account["liquidation_threshold_pct"],
                "collateral_assets": collateral_assets,
            })
        rows.extend(debts)
    return rows, notes, reports, locked
