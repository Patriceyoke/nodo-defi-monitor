"""Read-only ENS .eth expiry. Base Registrar nameExpires and NameWrapper getData.

No transactions. A name is warning from 30 days before expiry and danger while
it is expired, including the 90-day grace period and after grace ends.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from . import evm

log = logging.getLogger("monitor.ens")

BASE_REGISTRAR = "0x57f1887a8BF19b14fC0dF6Fd9B2acc9Af147eA85"
NAME_WRAPPER = "0xD4416b13d2b3a9aBae7AcD5D6C2BbDBE25686401"
GRACE_SECONDS = 90 * 86400
WARN_SECONDS = 30 * 86400
MAX_NAMES = 12
SELECTOR_NAME_EXPIRES = "d6e4fa86"
SELECTOR_GET_DATA = "0178fe3f"


def ens_status(expiry: int, now: int | None = None) -> dict[str, Any]:
    """Classify one expiry timestamp. Pure: used by tests and by the UI."""
    now = int(time.time() if now is None else now)
    expiry = int(expiry)
    seconds_left = expiry - now
    days_left = round(seconds_left / 86400, 2)
    grace_ends = expiry + GRACE_SECONDS
    in_grace = expiry <= now < grace_ends
    grace_ended = now >= grace_ends
    if grace_ended or in_grace or seconds_left <= 0:
        level = "danger"
    elif seconds_left <= WARN_SECONDS:
        level = "warning"
    else:
        level = "ok"
    return {
        "expiry": expiry,
        "grace_ends": grace_ends,
        "days_left": days_left,
        "in_grace": in_grace,
        "grace_ended": grace_ended,
        "level": level,
    }


def alert_text(name: str, status: dict[str, Any]) -> str:
    label = name or "un nome .eth"
    if status["grace_ended"]:
        return f"ENS {label} è scaduto e il periodo di grazia è finito."
    if status["in_grace"]:
        return f"ENS {label} è scaduto ed è nel periodo di grazia."
    days = max(0, int(status["days_left"]))
    return f"ENS {label} scade tra {days} giorni."


async def _expiry_calls(contract: str, token_ids: list[int], selector: str) -> list[int | None]:
    if not token_ids:
        return []
    calls = [
        (contract, "0x" + selector + int(token_id).to_bytes(32, "big").hex())
        for token_id in token_ids
    ]
    decoded = await evm.multicall("ethereum", calls)
    out: list[int | None] = []
    for ok, chunk in decoded:
        if not ok or len(chunk) < 32:
            out.append(None)
            continue
        out.append(int.from_bytes(chunk[:32], "big"))
    return out


async def _owned_token_ids(contract: str, holder: str) -> list[int]:
    balance = await evm.token_balance(contract, holder, "ethereum")
    count = min(int(balance), MAX_NAMES)
    if count <= 0:
        return []
    calls = []
    for index in range(count):
        data = (
            "0x"
            + evm.SELECTOR_TOKEN_OF_OWNER
            + evm._pad(holder)
            + index.to_bytes(32, "big").hex()
        )
        calls.append((contract, data))
    decoded = await evm.multicall("ethereum", calls)
    ids: list[int] = []
    for ok, chunk in decoded:
        if ok and len(chunk) >= 32:
            ids.append(int.from_bytes(chunk[:32], "big"))
    return ids


async def _display_name(contract: str, token_id: int) -> str:
    url = f"https://metadata.ens.domains/mainnet/{contract}/{token_id}"
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            response = await client.get(url)
        if response.status_code == 200:
            name = (response.json() or {}).get("name")
            if isinstance(name, str) and name.endswith(".eth"):
                return name
    except Exception as exc:  # noqa: BLE001
        log.info("Nome ENS %s non letto: %s", token_id, exc)
    text = str(token_id)
    return f"#{text[:6]}…{text[-4:]}" if len(text) > 12 else f"#{text}"


def _token_id_from_alchemy(raw: dict[str, Any]) -> int | None:
    token = raw.get("tokenId")
    if token is None:
        return None
    text = str(token)
    try:
        return int(text, 16) if text.startswith("0x") else int(text)
    except ValueError:
        return None


async def _discover_via_index(address: str) -> dict[str, list[int]]:
    """List only the two ENS contracts. Expiry is still read from the chain."""
    found: dict[str, list[int]] = {BASE_REGISTRAR.lower(): [], NAME_WRAPPER.lower(): []}
    try:
        from .nft import ALCHEMY_DEMO_KEY, _alchemy_key

        key, _source = _alchemy_key()
        key = key or ALCHEMY_DEMO_KEY
        url = (
            "https://eth-mainnet.g.alchemy.com/nft/v3/"
            f"{key}/getNFTsForOwner?owner={address}&withMetadata=false&pageSize={MAX_NAMES}"
            f"&contractAddresses[]={BASE_REGISTRAR}&contractAddresses[]={NAME_WRAPPER}"
        )
        async with httpx.AsyncClient(timeout=6.0) as client:
            response = await client.get(url)
        if response.status_code != 200:
            return found
        for raw in (response.json() or {}).get("ownedNfts") or []:
            contract = str((raw.get("contract") or {}).get("address") or "").lower()
            token_id = _token_id_from_alchemy(raw)
            if contract in found and token_id is not None and token_id not in found[contract]:
                found[contract].append(token_id)
            if sum(len(ids) for ids in found.values()) >= MAX_NAMES:
                break
    except Exception as exc:  # noqa: BLE001
        log.info("Elenco ENS non disponibile dall'indice: %s", exc)
    return found


async def _wrapper_expiries(token_ids: list[int]) -> dict[int, int]:
    if not token_ids:
        return {}
    calls = [
        (NAME_WRAPPER, "0x" + SELECTOR_GET_DATA + int(token_id).to_bytes(32, "big").hex())
        for token_id in token_ids
    ]
    decoded = await evm.multicall("ethereum", calls)
    out: dict[int, int] = {}
    for token_id, (ok, chunk) in zip(token_ids, decoded):
        if ok and len(chunk) >= 96:
            expiry = int.from_bytes(chunk[64:96], "big")
            if expiry:
                out[token_id] = expiry
    return out


async def names_for_wallet(address: str, owner: str, label: str) -> list[dict[str, Any]]:
    """Read .eth expiry on-chain. Discovery stays small and never loads the NFT section."""
    discovered = await _discover_via_index(address)
    registrar_ids = discovered[BASE_REGISTRAR.lower()]
    wrapper_ids = discovered[NAME_WRAPPER.lower()]
    if not registrar_ids:
        try:
            registrar_ids = await _owned_token_ids(BASE_REGISTRAR, address)
        except Exception as exc:  # noqa: BLE001
            log.info("ENS registrar per %s non letto: %s", label, exc)
            registrar_ids = []
    registrar_ids = registrar_ids[:MAX_NAMES]
    wrapper_ids = wrapper_ids[:MAX_NAMES]
    now = int(time.time())
    rows: list[dict[str, Any]] = []
    try:
        expiries = await _expiry_calls(BASE_REGISTRAR, registrar_ids, SELECTOR_NAME_EXPIRES)
    except Exception as exc:  # noqa: BLE001
        log.info("nameExpires non letta: %s", exc)
        expiries = []
    try:
        wrapped = await _wrapper_expiries(wrapper_ids)
    except Exception as exc:  # noqa: BLE001
        log.info("NameWrapper getData non letta: %s", exc)
        wrapped = {}
    pending: list[tuple[str, str, int, int]] = []
    for token_id, expiry in zip(registrar_ids, expiries):
        if expiry:
            pending.append((BASE_REGISTRAR, "registrar", token_id, expiry))
    for token_id, expiry in wrapped.items():
        pending.append((NAME_WRAPPER, "wrapper", token_id, expiry))
    for contract, kind, token_id, expiry in pending:
        status = ens_status(expiry, now)
        name = await _display_name(contract, token_id) if status["level"] != "ok" else None
        rows.append(
            {
                "owner": owner,
                "label": label,
                "address": address,
                "contract": contract,
                "token_id": str(token_id),
                "kind": kind,
                "name": name or "nome .eth",
                **status,
            }
        )
    return rows


async def scan_wallets(wallets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not wallets:
        return []
    batches = await asyncio.gather(
        *[names_for_wallet(w["address"], w["owner"], w.get("label") or w["address"]) for w in wallets],
        return_exceptions=True,
    )
    rows: list[dict[str, Any]] = []
    for batch in batches:
        if isinstance(batch, Exception):
            log.info("Scansione ENS saltata: %s", batch)
            continue
        rows.extend(batch)
    return rows


def alerts_for(names: list[dict[str, Any]]) -> list[dict[str, Any]]:
    alerts = []
    for item in names:
        if item.get("level") not in {"warning", "danger"}:
            continue
        alerts.append(
            {
                "owner": item.get("owner"),
                "label": item.get("label"),
                "name": item.get("name"),
                "level": item["level"],
                "days_left": item.get("days_left"),
                "in_grace": item.get("in_grace"),
                "grace_ended": item.get("grace_ended"),
                "expiry": item.get("expiry"),
                "text": alert_text(str(item.get("name") or ""), item),
            }
        )
    return alerts


async def annotate(items: list[dict[str, Any]]) -> None:
    """Attach expiry to .eth NFTs already loaded. Does not enumerate the wallet."""
    interesting = [
        item for item in items
        if str(item.get("contract") or "").lower() in {BASE_REGISTRAR.lower(), NAME_WRAPPER.lower()}
        and str(item.get("chain") or "ethereum") == "ethereum"
    ]
    if not interesting:
        return
    now = int(time.time())
    registrar = [item for item in interesting if str(item["contract"]).lower() == BASE_REGISTRAR.lower()]
    wrapper = [item for item in interesting if str(item["contract"]).lower() == NAME_WRAPPER.lower()]
    try:
        reg_exp = await _expiry_calls(
            BASE_REGISTRAR,
            [int(item["token_id"]) for item in registrar],
            SELECTOR_NAME_EXPIRES,
        )
        for item, expiry in zip(registrar, reg_exp):
            if expiry:
                item["ens"] = {"name": item.get("name"), **ens_status(expiry, now)}
    except Exception as exc:  # noqa: BLE001
        log.info("Scadenze registrar non allegate: %s", exc)
    if not wrapper:
        return
    try:
        calls = [
            (NAME_WRAPPER, "0x" + SELECTOR_GET_DATA + int(item["token_id"]).to_bytes(32, "big").hex())
            for item in wrapper
        ]
        decoded = await evm.multicall("ethereum", calls)
        for item, (ok, chunk) in zip(wrapper, decoded):
            if ok and len(chunk) >= 96:
                expiry = int.from_bytes(chunk[64:96], "big")
                if expiry:
                    item["ens"] = {"name": item.get("name"), **ens_status(expiry, now)}
    except Exception as exc:  # noqa: BLE001
        log.info("Scadenze wrapper non allegate: %s", exc)
