"""Read-only security checks. The app never signs, never revokes, never holds a key.

Approvals, protocol incidents and unexpected outflows are cached. The dashboard
does not wait for them: a daily task and a manual refresh fill the cache.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from . import db, evm, markets
from .protocols import TOKENS
from .validation import normalize_owner

log = logging.getLogger("monitor.security")

DAY = 86400
STALE_SECONDS = 180 * DAY
CACHE_TTL = DAY
UNLIMITED_RAW = 2**255
MEANINGFUL_USD = 50.0
VERY_LARGE_USD = 1_000.0
APPROVAL_TOPIC = "0x8c5be1e5ebec7d5bd14f71427d1e84f3dd0314c0f7b2291e5b200ac8c7c3b925"
APPROVAL_FOR_ALL_TOPIC = "0x17307eab39ab6107e8899845ad3d59bd9653f200f220920489ca2b5937696c31"
AAVE_POOL = "0x87870Bca3F3fD6335C3F4ce8392D69350B4fA4E2"
CHUNK_BLOCKS = 400_000
MAX_CHUNKS = 4
LOG_CAP = 30

WATCH_CHAINS = {
    "ethereum": {"id": 1, "explorer": "https://etherscan.io/tx/"},
    "bsc": {"id": 56, "explorer": "https://bscscan.com/tx/"},
    "polygon": {"id": 137, "explorer": "https://polygonscan.com/tx/"},
    "arbitrum": {"id": 42161, "explorer": "https://arbiscan.io/tx/"},
    "base": {"id": 8453, "explorer": "https://basescan.org/tx/"},
}

# Protocol name on a position -> words that identify it in a public incident feed.
PROTOCOL_NAMES = {
    "Lido": ("lido",),
    "Rocket Pool": ("rocket pool", "rocketpool"),
    "Aave": ("aave",),
    "Compound": ("compound",),
    "Fluid": ("fluid",),
    "Spark": ("spark savings", "spark"),
    "SparkLend": ("sparklend", "spark lend"),
    "Sky": ("sky", "maker"),
    "Maple": ("maple",),
}

_SCAN: asyncio.Task | None = None
_GOPLUS_BAD = ("phishing_activities", "stealing_attack", "honeypot_related_address", "cybercrime", "financial_crime", "gas_abuse")

# Spenders the app treats as current protocol contracts. Addresses are public
# deployments, compared in lowercase. A deprecated router stays in the list so
# an unlimited approval to it is called out instead of treated as normal.
PERMIT2 = "0x000000000022d473030f116ddee9f6b43ac78ba3"
_SPENDER_BOOK: dict[str, dict[str, Any]] = {
    # Aave v3 Pool (Ethereum).
    "0x87870bca3f3fd6335c3f4ce8392d69350b4fa4e2": {"protocol": "Aave", "active": True},
    # Compound v3 Comets (Ethereum): USDC, USDT, WETH.
    "0xc3d688b66703497daa19211eedff47f25384cdc3": {"protocol": "Compound", "active": True},
    "0x3afdc9bca9213a35503b077a6072f3d0d5ab0840": {"protocol": "Compound", "active": True},
    "0xa17581a9e3356d9a858b789d68b4d866e593ae94": {"protocol": "Compound", "active": True},
    # Lido: wstETH wrapper and the withdrawal queue.
    "0x7f39c581f595b53c5cb19bd0b3f8da6c935e2ca0": {"protocol": "Lido", "active": True},
    "0x889edc2edab5f40e902b864ad4d7ade8e412f9b1": {"protocol": "Lido", "active": True},
    # SparkLend pool and Spark savings USDC.
    "0xc13e21b648a5ee794902342038ff3adab66be987": {"protocol": "Spark", "active": True},
    "0x28b3a8fb53b741a8fd78c0fb9a6b2393d896a43d": {"protocol": "Spark", "active": True},
    # Sky savings and staking vaults.
    "0xa3931d71877c0e7a3148cb7eb4463524fec27fbd": {"protocol": "Sky", "active": True},
    "0x99cd4ec3f88a45940936f469e4bb72a2a701eeb9": {"protocol": "Sky", "active": True},
    # Fluid liquidity layer.
    "0x52aa899454998be5b000ad077a46bbe360f4e497": {"protocol": "Fluid", "active": True},
    # Uniswap Permit2 (same address on the chains this app watches) and the current routers.
    PERMIT2: {"protocol": "Uniswap", "active": True, "permit2": True},
    "0x3fc91a3afd70395cd496c647d5a6cc9d4b2b7fad": {"protocol": "Uniswap", "active": True},
    "0x68b3465833fb72a70ecdf485e0e4c7bd8665fc45": {"protocol": "Uniswap", "active": True},
    # Older routers. An approval here is not "in use" even though the contract is famous.
    "0x7a250d5630b4cf539739df2c5dacb4c659f2488d": {"protocol": "Uniswap", "active": False, "deprecated": True},
    "0xe592427a0aece92de3edee1f18e0157c05861564": {"protocol": "Uniswap", "active": False, "deprecated": True},
}


def is_unlimited(raw: int) -> bool:
    return int(raw) >= UNLIMITED_RAW


def risk_usd(allowance_raw: int, balance_raw: int, decimals: int, price: float) -> float:
    """Dollars that could move now: the allowance, but never more than the balance."""
    allowance_raw = max(0, int(allowance_raw))
    balance_raw = max(0, int(balance_raw))
    capped = balance_raw if is_unlimited(allowance_raw) else min(allowance_raw, balance_raw)
    units = capped / float(10 ** max(0, int(decimals)))
    return units * max(0.0, float(price or 0))


def approval_flags(
    *,
    unlimited: bool,
    code_empty: bool,
    verified: bool | None,
    named: bool,
    approved_at: int | None,
    now: int,
    exploited: bool,
) -> list[str]:
    flags: list[str] = []
    if unlimited:
        flags.append("unlimited")
    if code_empty:
        flags.append("eoa")
    elif verified is False:
        flags.append("unverified")
    if not named and not code_empty and verified is not True:
        flags.append("unknown")
    if approved_at and now - int(approved_at) > STALE_SECONDS:
        flags.append("stale")
    if exploited:
        flags.append("exploited")
    return flags


def approval_severity(flags: list[str]) -> str:
    if "exploited" in flags or ("unlimited" in flags and any(flag in flags for flag in ("eoa", "unverified", "unknown"))):
        return "danger"
    if flags:
        return "warning"
    return "ok"


def known_spender(address: str) -> dict[str, Any] | None:
    return _SPENDER_BOOK.get((address or "").lower())


def _uses_protocol(positions: list[dict[str, Any]] | None, owner: str | None, protocol: str) -> bool:
    for row in positions or []:
        if owner and row.get("owner") not in (None, "", owner):
            continue
        if row.get("protocol") != protocol:
            continue
        try:
            if float(row.get("usd") or 0) > 0:
                return True
        except (TypeError, ValueError):
            continue
    return False


def _allowance_is_broad(row: dict[str, Any]) -> bool:
    flags = row.get("flags") or []
    if "unlimited" in flags or row.get("allowance") == "unlimited":
        return True
    risk = row.get("value_at_risk_usd")
    try:
        if risk is not None and float(risk) >= VERY_LARGE_USD:
            return True
    except (TypeError, ValueError):
        pass
    allowance = row.get("allowance")
    try:
        if int(allowance) >= 10**30:
            return True
    except (TypeError, ValueError):
        pass
    return False


def _holds_meaningful_value(row: dict[str, Any]) -> bool:
    risk = row.get("value_at_risk_usd")
    try:
        if risk is not None and float(risk) >= MEANINGFUL_USD:
            return True
    except (TypeError, ValueError):
        pass
    return False


def classify_approval(row: dict[str, Any], positions: list[dict[str, Any]] | None = None) -> tuple[str, str]:
    """Return ``rischiosa`` or ``normale``, and a short Italian reason.

    A known, current protocol contract is normal when the user is actually
    using that protocol, or when the spender is Permit2 and the token still
    has value. Anything unlimited or large toward another spender, or any
    spender that is an EOA, unverified, flagged or a retired router, is a
    revoke.
    """
    flags = list(row.get("flags") or [])
    book = known_spender(str(row.get("spender") or ""))
    owner = row.get("owner")
    broad = _allowance_is_broad(row)
    holds = _holds_meaningful_value(row)
    if "exploited" in flags:
        return "rischiosa", "Lo spender risulta segnalato o coinvolto in un exploit."
    if "eoa" in flags:
        return "rischiosa", "Lo spender è un indirizzo semplice, non un contratto di protocollo."
    if "unverified" in flags:
        return "rischiosa", "Lo spender non è un contratto verificato."
    if book and book.get("deprecated"):
        return "rischiosa", "È un router vecchio, non quello che i protocolli usano ora."
    in_use = bool(book and book.get("active") and _uses_protocol(positions, owner, str(book.get("protocol") or "")))
    permit2_in_use = bool(book and book.get("permit2") and (holds or in_use or broad))
    if book and book.get("active") and (in_use or permit2_in_use):
        return "normale", f"Contratto attivo di {book.get('protocol')} per un token che stai usando."
    if broad and not (book and book.get("active") and in_use):
        if book and book.get("active"):
            return "rischiosa", f"Permesso ampio verso {book.get('protocol')}, ma quel protocollo non risulta in uso."
        return "rischiosa", "Permesso ampio verso un contratto che non è un protocollo che stai usando."
    if holds and not (book and book.get("active") and in_use):
        return "rischiosa", "Tieni ancora questo token e lo spender non è un protocollo attivo che stai usando."
    if book and book.get("active"):
        return "normale", f"Contratto attivo di {book.get('protocol')}."
    return "normale", "Permesso piccolo, senza un segnale di rischio."


def annotate_risk(rows: list[dict[str, Any]], positions: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Attach the revoke class. Rischiose come first; hidden rows stay in the list."""
    labelled: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        kind, reason = classify_approval(item, positions)
        item["risk_class"] = kind
        item["risk_reason"] = reason
        item["severity"] = "danger" if kind == "rischiosa" else "ok"
        labelled.append(item)
    labelled.sort(key=lambda item: (0 if item.get("risk_class") == "rischiosa" else 1, item.get("symbol") or ""))
    return labelled


def _portfolio_positions() -> list[dict[str, Any]]:
    stored = db.cache_get("portfolio") or {}
    positions = stored.get("positions") if isinstance(stored, dict) else None
    return list(positions or [])


def _risky_visible(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in rows if row.get("risk_class") == "rischiosa" and not row.get("hidden")]


def revoke_url(wallet: str, chain_id: int) -> str:
    return f"https://revoke.cash/address/{wallet}?chainId={int(chain_id)}"


def tx_url(chain: str, tx_hash: str | None) -> str | None:
    if not tx_hash:
        return None
    base = WATCH_CHAINS.get(chain, {}).get("explorer")
    if not base:
        return None
    return base + tx_hash


def _topic_address(topic: str) -> str:
    raw = topic[2:] if topic.startswith("0x") else topic
    return "0x" + raw[-40:].lower()


def parse_approval_log(entry: dict[str, Any]) -> dict[str, Any] | None:
    """One Approval or ApprovalForAll log. Revoked rows (amount 0 / false) are returned too."""
    topics = [str(item) for item in (entry.get("topics") or [])]
    if not topics:
        return None
    topic0 = topics[0].lower()
    data = str(entry.get("data") or "0x")
    block = entry.get("blockNumber") or entry.get("block_number") or "0x0"
    block_i = int(block, 16) if isinstance(block, str) and str(block).startswith("0x") else int(block or 0)
    stamp = entry.get("timeStamp") or entry.get("timestamp")
    approved_at = None
    if stamp not in (None, ""):
        approved_at = int(stamp, 16) if isinstance(stamp, str) and str(stamp).startswith("0x") else int(stamp)
    tx_hash = entry.get("transactionHash") or entry.get("tx_hash")
    token = str(entry.get("address") or "").lower()
    if topic0 == APPROVAL_TOPIC and len(topics) >= 3:
        if len(topics) >= 4:
            kind, amount, token_id = "erc721", None, int(topics[3], 16)
            active = True
        else:
            kind, token_id = "erc20", None
            amount = int(data, 16) if data not in {"0x", "0x0", ""} else 0
            active = amount != 0
        return {
            "kind": kind,
            "token": token,
            "spender": _topic_address(topics[2]),
            "amount": amount,
            "token_id": token_id,
            "active": active,
            "block": block_i,
            "approved_at": approved_at,
            "tx_hash": tx_hash,
        }
    if topic0 == APPROVAL_FOR_ALL_TOPIC and len(topics) >= 3:
        active = int(data, 16) == 1 if data not in {"0x", "0x0", ""} else False
        return {
            "kind": "erc1155" if "1155" in str(entry.get("tokenType") or "").lower() else "nft-operator",
            "token": token,
            "spender": _topic_address(topics[2]),
            "amount": None,
            "token_id": None,
            "active": active,
            "block": block_i,
            "approved_at": approved_at,
            "tx_hash": tx_hash,
        }
    return None


def latest_approvals(logs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    chosen: dict[tuple[str, str, str], dict[str, Any]] = {}
    for entry in logs:
        parsed = parse_approval_log(entry)
        if not parsed:
            continue
        key = (parsed["token"], parsed["spender"])
        current = chosen.get(key)
        if current is None or parsed["block"] >= current["block"]:
            chosen[key] = parsed
    return [row for row in chosen.values() if row["active"]][:LOG_CAP]


def aave_frozen_paused(configuration: int) -> tuple[bool, bool]:
    """Aave v3 reserve bitmap: bit 57 frozen, bit 60 paused."""
    value = int(configuration)
    return bool((value >> 57) & 1), bool((value >> 60) & 1)


def held_protocols(positions: list[dict[str, Any]]) -> set[str]:
    return {str(row.get("protocol") or "") for row in positions if row.get("protocol") and row.get("protocol") != "Wallet"}


def match_hacks(held: set[str], hacks: list[dict[str, Any]], now: int) -> list[dict[str, Any]]:
    """Incidents only for protocols the wallets actually hold, and only if recent."""
    found: list[dict[str, Any]] = []
    for hack in hacks:
        stamp = int(hack.get("date") or 0)
        if not stamp or now - stamp > STALE_SECONDS:
            continue
        name = str(hack.get("name") or "")
        blob = name.lower()
        for protocol in held:
            words = PROTOCOL_NAMES.get(protocol, (protocol.lower(),))
            if not any(word and word in blob for word in words):
                continue
            age = now - stamp
            found.append({
                "protocol": protocol,
                "name": name,
                "severity": "danger" if age <= 30 * DAY else "warning",
                "date": stamp,
                "detail": str(hack.get("technique") or hack.get("classification") or "incidente segnalato"),
                "amount_usd": hack.get("amount"),
                "source": "DefiLlama",
            })
            break
    return found


def compare_balance(
    previous: dict[str, Any] | None,
    amount: float,
    usd: float,
    drop_pct: float,
    usd_min: float,
) -> str | None:
    """None on the first sighting. A later drop can be a percent move, a dollar move, or both."""
    if previous is None:
        return None
    prev_amount = float(previous.get("amount") or 0)
    if prev_amount <= 0 or amount >= prev_amount - 1e-12:
        return None
    frac = (prev_amount - amount) / prev_amount
    prev_usd = float(previous.get("usd") or 0)
    usd_drop = frac * prev_usd
    reasons: list[str] = []
    if frac * 100 >= float(drop_pct):
        reasons.append("percent")
    if usd_drop >= float(usd_min):
        reasons.append("usd")
    return "+".join(reasons) or None


def _pad_address(address: str) -> str:
    return address.lower().replace("0x", "").zfill(64)


def _mark_key(wallet: str, chain: str, token: str) -> str:
    return f"{wallet.lower()}|{chain}|{token.lower()}"


def outflow_rows(
    positions: list[dict[str, Any]],
    marks: list[dict[str, Any]],
    wallets: set[str],
    drop_pct: float,
    usd_min: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return alerts and the marks to store. The first observation is stored and not alerted."""
    known = {_mark_key(row["wallet"], row["chain"], row["token"]): row for row in marks}
    seen: set[str] = set()
    alerts: list[dict[str, Any]] = []
    updates: list[dict[str, Any]] = []
    rows = list(positions)
    for row in rows:
        wallet = str(row.get("address") or "")
        if wallet.lower() not in wallets:
            continue
        token = str(row.get("token") or row.get("asset") or "")
        chain = str(row.get("chain") or "ethereum")
        key = _mark_key(wallet, chain, token)
        seen.add(key)
        amount = float(row.get("amount") or 0)
        usd = float(row.get("usd") or 0)
        reason = compare_balance(known.get(key), amount, usd, drop_pct, usd_min)
        if reason:
            alerts.append({
                "owner": row.get("owner"),
                "label": row.get("label"),
                "wallet": wallet,
                "chain": chain,
                "token": token,
                "symbol": row.get("asset") or row.get("symbol") or token,
                "previous": float(known[key]["amount"]),
                "amount": amount,
                "usd": usd,
                "reason": reason,
                "severity": "danger" if "usd" in reason else "warning",
            })
        updates.append({
            "wallet": wallet, "chain": chain, "token": token, "owner": row.get("owner") or "",
            "symbol": str(row.get("asset") or ""), "amount": amount, "usd": usd,
        })
    for key, mark in known.items():
        if key in seen or mark["wallet"].lower() not in wallets:
            continue
        if float(mark.get("amount") or 0) <= 0:
            continue
        reason = compare_balance(mark, 0.0, 0.0, drop_pct, usd_min)
        if reason:
            alerts.append({
                "owner": mark.get("owner"),
                "label": mark.get("symbol"),
                "wallet": mark["wallet"],
                "chain": mark["chain"],
                "token": mark["token"],
                "symbol": mark.get("symbol"),
                "previous": float(mark["amount"]),
                "amount": 0.0,
                "usd": 0.0,
                "reason": reason,
                "severity": "danger" if "usd" in reason else "warning",
            })
        updates.append({
            "wallet": mark["wallet"], "chain": mark["chain"], "token": mark["token"],
            "owner": mark.get("owner") or "", "symbol": mark.get("symbol") or "",
            "amount": 0.0, "usd": 0.0,
        })
    return alerts, updates


def approval_identity(row: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(row.get("wallet") or "").lower(),
        str(row.get("chain") or "").lower(),
        str(row.get("token") or "").lower(),
        str(row.get("spender") or "").lower(),
    )


def approval_key(row: dict[str, Any]) -> str:
    return "|".join(approval_identity(row))


def risky_keys(rows: list[dict[str, Any]]) -> list[str]:
    return sorted({approval_key(row) for row in _risky_visible(rows) if approval_key(row).strip("|")})


def _key_part(value: Any) -> str:
    text = str(value or "").replace("|", " ").strip().lower()
    return (text or "-")[:80]


def protocol_is_urgent(item: dict[str, Any]) -> bool:
    """A held-asset peg break. High utilization and missing data stay informational."""
    if item.get("tone") != "danger":
        return False
    label = str(item.get("label") or "")
    title = str(item.get("title") or "")
    return label == "Peg allontanato" or title.startswith("Peg ")


def incident_is_urgent(item: dict[str, Any]) -> bool:
    """Recent exploit or an on-chain pause. Older warnings stay in the list only."""
    return item.get("severity") == "danger"


def ens_is_urgent(item: dict[str, Any]) -> bool:
    """Expired or in the grace period. A name that merely expires soon is not urgent."""
    return item.get("level") == "danger"


def protocol_urgent_key(item: dict[str, Any]) -> str:
    return "|".join(("protocol", "peg", _key_part(item.get("protocol")), "danger"))


def incident_urgent_key(item: dict[str, Any]) -> str:
    return "|".join(("incident", _key_part(item.get("protocol")), _key_part(item.get("name") or item.get("detail")), "danger"))


def outflow_urgent_key(item: dict[str, Any]) -> str:
    return "|".join(("outflow", _key_part(item.get("wallet")), _key_part(item.get("chain")), _key_part(item.get("token") or item.get("symbol"))))


def ens_urgent_key(item: dict[str, Any]) -> str:
    return "|".join(("ens", _key_part(item.get("owner") or "tutti"), _key_part(item.get("name")), "danger"))


def _four_part(keys: list[str]) -> list[str]:
    return sorted({key for key in keys if key.count("|") == 3 and all(len(part) <= 80 for part in key.split("|"))})


def heavy_urgent_keys(approvals: list[dict[str, Any]], incidents: list[dict[str, Any]], outflows: list[dict[str, Any]]) -> list[str]:
    keys = list(risky_keys(approvals))
    keys.extend(incident_urgent_key(row) for row in incidents if incident_is_urgent(row))
    keys.extend(outflow_urgent_key(row) for row in outflows)
    return _four_part(keys)


def annotate_light_alerts(
    protocols: list[dict[str, Any]],
    ens_alerts: list[dict[str, Any]],
    summaries: dict[str, Any],
    positions: list[dict[str, Any]],
    view_owner: str,
) -> None:
    """Mark informational rows and add their urgent keys to each owner summary."""
    for row in protocols:
        row["urgent"] = protocol_is_urgent(row)
        if row["urgent"]:
            row["urgent_key"] = protocol_urgent_key(row)
    for row in ens_alerts:
        row["urgent"] = ens_is_urgent(row)
        if row["urgent"]:
            row["urgent_key"] = ens_urgent_key(row)

    def held(owner: str) -> set[str]:
        names: set[str] = set()
        for row in positions:
            if owner != "tutti" and row.get("owner") != owner:
                continue
            if row.get("protocol"):
                names.add(str(row["protocol"]))
            if row.get("asset"):
                names.add(str(row["asset"]))
        return names

    owners = {"io": "io", "moglie": "moglie", "tutti": "tutti", "view": view_owner}
    for slot, owner in owners.items():
        summary = summaries.get(slot)
        if not isinstance(summary, dict):
            continue
        keys = list(summary.get("urgent_keys") or [])
        names = held(owner)
        for row in protocols:
            if row.get("urgent") and str(row.get("protocol") or "") in names:
                keys.append(str(row.get("urgent_key") or ""))
        for row in ens_alerts:
            if row.get("urgent") and (owner == "tutti" or row.get("owner") == owner):
                keys.append(str(row.get("urgent_key") or ""))
        unique = _four_part(keys)
        summary["urgent_keys"] = unique
        summary["urgent"] = len(unique)


def banner_has_new(current: list[str], dismissed: list[str] | None) -> bool:
    """True when a risky approval is on screen that was not in the dismissed set."""
    known = set(dismissed or [])
    return any(key and key not in known for key in current)


def dedupe_approvals(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per wallet, chain, token and spender."""
    rank = {"ok": 0, "warning": 1, "danger": 2}
    chosen: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for row in rows:
        key = approval_identity(row)
        previous = chosen.get(key)
        if previous is None:
            chosen[key] = row
            continue
        better_severity = rank.get(str(row.get("severity")), 0) > rank.get(str(previous.get("severity")), 0)
        newer = int(row.get("approved_at") or 0) >= int(previous.get("approved_at") or 0)
        if better_severity or (rank.get(str(row.get("severity")), 0) == rank.get(str(previous.get("severity")), 0) and newer):
            chosen[key] = row
    return list(chosen.values())


def annotate_hidden(rows: list[dict[str, Any]], hides: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """A hide sticks only while the allowance is unchanged."""
    stored = {
        (row["wallet"], row["chain"], row["token"], row["spender"]): str(row.get("allowance"))
        for row in (hides if hides is not None else db.list_approval_hides())
    }
    out: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        key = approval_identity(item)
        remembered = stored.get(key)
        item["hidden"] = remembered is not None and remembered == str(item.get("allowance"))
        out.append(item)
    return out


def security_summary(owner: str = "tutti") -> dict[str, Any]:
    owner = normalize_owner(owner, allow_all=True)
    report = cached_report() or {}
    approvals = annotate_risk(
        annotate_hidden(dedupe_approvals(filter_owner_rows(list(report.get("approvals") or []), owner))),
        _portfolio_positions(),
    )
    incidents = list(report.get("incidents") or [])
    outflows = filter_owner_rows(list(report.get("outflows") or []), owner)
    if owner != "tutti":
        incidents = [row for row in incidents if owner in (row.get("owners") or [])]
    visible_approvals = _risky_visible(approvals)
    urgent = heavy_urgent_keys(visible_approvals, incidents, outflows)
    return {
        "approvals": len(visible_approvals),
        "hidden": sum(1 for row in approvals if row.get("hidden")),
        "incidents": len(incidents),
        "outflows": len(outflows),
        "visible": len(visible_approvals) + len(incidents) + len(outflows),
        "risky_keys": risky_keys(approvals),
        "urgent_keys": urgent,
        "urgent": len(urgent),
    }


def filter_owner_rows(rows: list[dict[str, Any]], owner: str) -> list[dict[str, Any]]:
    owner = normalize_owner(owner, allow_all=True)
    if owner == "tutti":
        return rows
    return [row for row in rows if row.get("owner") == owner]


def _thresholds() -> tuple[float, float]:
    try:
        drop = float(db.get_setting("outflow_drop_pct") or "5")
    except ValueError:
        drop = 5.0
    try:
        usd = float(db.get_setting("outflow_usd") or "500")
    except ValueError:
        usd = 500.0
    return max(0.1, min(drop, 100.0)), max(0.0, usd)


def cached_report() -> dict[str, Any] | None:
    stored = db.cache_get("security")
    if not isinstance(stored, dict):
        return None
    return stored


def cached_alerts(owner: str = "tutti") -> list[dict[str, Any]]:
    report = cached_report()
    if not report:
        return []
    return filter_owner_rows(list(report.get("alerts") or []), owner)


def _alert_from_approval(row: dict[str, Any]) -> dict[str, Any] | None:
    if row.get("risk_class") != "rischiosa" and row.get("severity") not in {"warning", "danger"}:
        return None
    if row.get("risk_class") == "normale":
        return None
    flags = row.get("flags") or []
    bits = []
    if "unlimited" in flags:
        bits.append("illimitata")
    if "exploited" in flags:
        bits.append("spender segnalato")
    if "eoa" in flags:
        bits.append("spender è un indirizzo semplice")
    elif "unverified" in flags:
        bits.append("spender non verificato")
    if "stale" in flags:
        bits.append("non aggiornata da oltre 180 giorni")
    risk = row.get("value_at_risk_usd")
    extra = f" Valore a rischio circa {risk:.0f} USD." if isinstance(risk, (int, float)) and risk else ""
    reason = row.get("risk_reason") or (", ".join(bits) if bits else "da revocare")
    return {
        "owner": row.get("owner"),
        "level": "danger",
        "kind": "approval",
        "title": f"Revoca {row.get('symbol') or 'token'}",
        "text": f"{row.get('label') or 'Portafoglio'}: {reason}{extra} La revoca si firma sul tuo hardware, non qui.",
        "href": row.get("revoke_url"),
    }


def _alert_from_incident(row: dict[str, Any], owner: str) -> dict[str, Any]:
    return {
        "owner": owner,
        "level": row.get("severity") or "warning",
        "kind": "incident",
        "title": f"Protocollo {row.get('protocol')}",
        "text": f"{row.get('name') or row.get('protocol')}: {row.get('detail') or 'controllo'}.",
        "href": None,
    }


def _alert_from_outflow(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "owner": row.get("owner"),
        "level": row.get("severity") or "warning",
        "kind": "outflow",
        "title": f"Uscita {row.get('symbol') or ''}".strip(),
        "text": (
            f"{row.get('label') or 'Portafoglio'} · {row.get('chain')}: "
            f"da {row.get('previous')} a {row.get('amount')}."
        ),
        "href": row.get("tx_url"),
    }


async def _routescan_logs(chain_id: int, topic0: str, wallet: str, start: int, end: int) -> list[dict[str, Any]]:
    topic1 = "0x" + _pad_address(wallet)
    url = (
        f"https://api.routescan.io/v2/network/mainnet/evm/{chain_id}/etherscan/api"
        f"?module=logs&action=getLogs&fromBlock={start}&toBlock={end}&topic0={topic0}&topic1={topic1}"
    )
    async with httpx.AsyncClient(timeout=12.0) as client:
        response = await client.get(url, headers={"User-Agent": "monitor-defi/0.2 (read-only)"})
        response.raise_for_status()
        payload = response.json()
    result = payload.get("result") if isinstance(payload, dict) else None
    return result if isinstance(result, list) else []


async def _head_block(chain: str) -> int:
    raw = await evm.eth_rpc("eth_blockNumber", [], chain=chain)
    return int(raw, 16)


async def _approval_logs(chain: str, wallet: str) -> tuple[list[dict[str, Any]], bool]:
    chain_id = WATCH_CHAINS[chain]["id"]
    head = await _head_block(chain)
    partial = False
    found: list[dict[str, Any]] = []
    for topic in (APPROVAL_TOPIC, APPROVAL_FOR_ALL_TOPIC):
        cursor = head
        reached_start = False
        for _ in range(MAX_CHUNKS):
            start = max(0, cursor - CHUNK_BLOCKS)
            try:
                found.extend(await _routescan_logs(chain_id, topic, wallet, start, cursor))
            except Exception as exc:  # noqa: BLE001
                log.info("Log approvazioni non letti su %s (%s).", chain, type(exc).__name__)
                partial = True
                break
            if start == 0:
                reached_start = True
                break
            cursor = start - 1
        partial = partial or not reached_start
    return found, partial


async def _code_empty(chain: str, spender: str) -> bool:
    try:
        code = await evm.eth_rpc("eth_getCode", [spender, "latest"], chain=chain)
    except Exception:
        return False
    return code in (None, "0x", "0x0")


async def _allowance(chain: str, token: str, owner: str, spender: str, kind: str) -> int | None:
    if kind != "erc20":
        data = "0xe985e9c5" + _pad_address(owner) + _pad_address(spender)
        try:
            raw = await evm.eth_call_raw(token, data, chain)
        except Exception:
            return None
        return 1 if int(raw or "0x0", 16) == 1 else 0
    data = "0xdd62ed3e" + _pad_address(owner) + _pad_address(spender)
    try:
        return await evm.eth_call(token, data, chain)
    except Exception:
        return None


async def _balance(chain: str, token: str, owner: str) -> int:
    try:
        return await evm.token_balance(token, owner, chain)
    except Exception:
        return 0


async def _goplus_bad(chain_id: int, spender: str) -> bool:
    url = f"https://api.gopluslabs.io/api/v1/address_security/{spender}?chain_id={chain_id}"
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            response = await client.get(url, headers={"User-Agent": "monitor-defi/0.2"})
            response.raise_for_status()
            result = (response.json().get("result") or {})
    except Exception:
        return False
    row = result.get(spender.lower()) or result.get(spender) or {}
    if not isinstance(row, dict):
        return False
    return any(str(row.get(key) or "0") == "1" for key in _GOPLUS_BAD)


async def _hacks() -> list[dict[str, Any]]:
    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            response = await client.get("https://api.llama.fi/hacks", headers={"User-Agent": "monitor-defi/0.2"})
            response.raise_for_status()
            data = response.json()
    except Exception as exc:  # noqa: BLE001
        log.info("Feed incidenti non letto (%s).", type(exc).__name__)
        return []
    return data if isinstance(data, list) else []


def _rss_items(xml_text: str, protocol: str) -> list[dict[str, Any]]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    items: list[dict[str, Any]] = []
    for node in root.iter("item"):
        title = (node.findtext("title") or "").strip()
        if protocol.lower() not in title.lower():
            continue
        stamp = 0
        pub = node.findtext("pubDate") or ""
        try:
            stamp = int(parsedate_to_datetime(pub).timestamp())
        except (TypeError, ValueError, OverflowError):
            stamp = 0
        items.append({"name": title, "date": stamp, "technique": "comunicazione ufficiale", "source": protocol})
    return items


async def _official_notes(held: set[str]) -> list[dict[str, Any]]:
    feeds = {"Lido": "https://blog.lido.fi/rss/"}
    notes: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=8.0) as client:
        for protocol, url in feeds.items():
            if protocol not in held:
                continue
            try:
                response = await client.get(url, headers={"User-Agent": "monitor-defi/0.2"})
                response.raise_for_status()
                notes.extend(_rss_items(response.text, protocol))
            except Exception:
                continue
    return notes


async def _pause_flags(positions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    incidents: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for row in positions:
        protocol = str(row.get("protocol") or "")
        chain = str(row.get("chain") or "")
        key = (protocol, str(row.get("asset") or ""))
        if key in seen or chain != "ethereum":
            continue
        seen.add(key)
        if protocol == "Aave":
            underlying = ""
            for spec in TOKENS:
                if spec.get("protocol") == "Aave" and spec.get("asset") == row.get("asset"):
                    underlying = str(spec.get("price") or "")
                    break
            if not underlying:
                continue
            data = "0xc44b11f7" + _pad_address(underlying)
            try:
                raw = await evm.eth_call(AAVE_POOL, data, "ethereum")
            except Exception:
                continue
            frozen, paused = aave_frozen_paused(raw)
            if frozen or paused:
                state = "in pausa" if paused else "congelata"
                incidents.append({
                    "protocol": "Aave",
                    "name": f"Aave v3 {row.get('asset')}",
                    "severity": "danger",
                    "date": int(time.time()),
                    "detail": f"La riserva risulta {state} on-chain.",
                    "source": "Aave Pool",
                })
        elif protocol == "Compound":
            token = str(row.get("token") or "")
            if not token:
                continue
            try:
                supply = await evm.eth_call(token, "0x0bc47ad1", "ethereum")
                withdraw = await evm.eth_call(token, "0x67800b5f", "ethereum")
            except Exception:
                continue
            if supply or withdraw:
                incidents.append({
                    "protocol": "Compound",
                    "name": f"Compound v3 {row.get('asset')}",
                    "severity": "danger",
                    "date": int(time.time()),
                    "detail": "Il mercato risulta in pausa on-chain.",
                    "source": "Compound Comet",
                })
        elif protocol == "Fluid":
            token = str(row.get("token") or "")
            if not token:
                continue
            try:
                paused = await evm.eth_call(token, "0x5c975abb", "ethereum")
            except Exception:
                continue
            if paused:
                incidents.append({
                    "protocol": "Fluid",
                    "name": f"Fluid {row.get('asset')}",
                    "severity": "danger",
                    "date": int(time.time()),
                    "detail": "Il contratto risulta in pausa on-chain.",
                    "source": "Fluid",
                })
    return incidents


async def _recent_out_tx(chain: str, wallet: str) -> str | None:
    try:
        head = await _head_block(chain)
        logs = await _routescan_logs(
            WATCH_CHAINS[chain]["id"],
            "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
            wallet,
            max(0, head - 50_000),
            head,
        )
    except Exception:
        return None
    for entry in reversed(logs):
        topics = entry.get("topics") or []
        if len(topics) >= 2 and _topic_address(topics[1]) == wallet.lower():
            return entry.get("transactionHash")
    return logs[-1].get("transactionHash") if logs else None


async def scan_and_store() -> dict[str, Any]:
    """One full read. Safe to run in the background. Never sends a transaction."""
    now = int(time.time())
    wallets = db.list_evm()
    portfolio = db.cache_get("portfolio") or {}
    positions = list(portfolio.get("positions") or [])
    held = held_protocols(positions)
    drop_pct, usd_min = _thresholds()
    approvals: list[dict[str, Any]] = []
    errors: list[str] = []
    partial = False
    spender_bad: dict[str, bool] = {}
    for wallet in wallets:
        address = wallet["address"]
        for chain in WATCH_CHAINS:
            try:
                logs, chunk_partial = await _approval_logs(chain, address)
            except Exception as exc:  # noqa: BLE001
                log.info("Approvazioni %s non lette (%s).", chain, type(exc).__name__)
                errors.append(f"Approvazioni su {chain} non lette.")
                continue
            partial = partial or chunk_partial
            for item in latest_approvals(logs):
                current = await _allowance(chain, item["token"], address, item["spender"], item["kind"])
                if current == 0:
                    continue
                amount = current if current is not None else int(item.get("amount") or 0)
                unlimited = item["kind"] == "erc20" and is_unlimited(amount)
                code_empty = await _code_empty(chain, item["spender"])
                bad_key = item["spender"].lower()
                if bad_key not in spender_bad:
                    spender_bad[bad_key] = await _goplus_bad(WATCH_CHAINS[chain]["id"], item["spender"])
                balance = await _balance(chain, item["token"], address) if item["kind"] == "erc20" else 0
                price = 0.0
                if item["kind"] == "erc20":
                    try:
                        prices = await markets.prices_usd([f"{chain}:{item['token']}"])
                        price = float(prices.get(f"{chain}:{item['token'].lower()}") or 0)
                    except Exception:
                        price = 0.0
                decimals = 18
                for spec in TOKENS:
                    if spec.get("token", "").lower() == item["token"].lower():
                        decimals = int(spec.get("decimals") or 18)
                        break
                risk = risk_usd(amount, balance, decimals, price) if item["kind"] == "erc20" else None
                known = item["spender"].lower() == "0x000000000022d473030f116ddee9f6b43ac78ba3"
                flags = approval_flags(
                    unlimited=unlimited,
                    code_empty=code_empty,
                    verified=True if known else None,
                    named=known,
                    approved_at=item.get("approved_at"),
                    now=now,
                    exploited=spender_bad.get(bad_key, False),
                )
                draft = {
                    "owner": wallet["owner"],
                    "label": wallet["label"],
                    "wallet": address,
                    "chain": chain,
                    "chain_id": WATCH_CHAINS[chain]["id"],
                    "kind": item["kind"],
                    "token": item["token"],
                    "symbol": next((spec["symbol"] for spec in TOKENS if spec.get("token", "").lower() == item["token"].lower()), "token"),
                    "spender": item["spender"],
                    "allowance": "unlimited" if unlimited else amount,
                    "value_at_risk_usd": risk,
                    "flags": flags,
                    "severity": approval_severity(flags),
                    "approved_at": item.get("approved_at"),
                    "revoke_url": revoke_url(address, WATCH_CHAINS[chain]["id"]),
                    "tx_url": tx_url(chain, item.get("tx_hash")),
                }
                risk_class, risk_reason = classify_approval(draft, positions)
                draft["risk_class"] = risk_class
                draft["risk_reason"] = risk_reason
                draft["severity"] = "danger" if risk_class == "rischiosa" else "ok"
                approvals.append(draft)
    hacks = await _hacks()
    incidents = match_hacks(held, hacks, now)
    incidents.extend(match_hacks(held, await _official_notes(held), now))
    try:
        incidents.extend(await _pause_flags(positions))
    except Exception as exc:  # noqa: BLE001
        log.info("Stato on-chain non letto (%s).", type(exc).__name__)
        errors.append("Stato on-chain dei protocolli non letto.")
    wallet_set = {row["address"].lower() for row in wallets}
    outflows, updates = outflow_rows(positions, db.list_balance_marks(), wallet_set, drop_pct, usd_min)
    for row in outflows:
        tx_hash = await _recent_out_tx(row["chain"], row["wallet"])
        row["tx_url"] = tx_url(row["chain"], tx_hash)
    for row in updates:
        db.upsert_balance_mark(row["wallet"], row["chain"], row["token"], row["owner"], row["symbol"], row["amount"], row["usd"])
    alerts: list[dict[str, Any]] = []
    approvals = annotate_risk(dedupe_approvals(approvals), positions)
    alerts.extend(item for item in (_alert_from_approval(row) for row in approvals) if item)
    for incident in incidents:
        owners = sorted({pos.get("owner") for pos in positions if pos.get("protocol") == incident.get("protocol")})
        incident["owners"] = owners
        for owner in owners:
            alerts.append(_alert_from_incident(incident, str(owner)))
    alerts.extend(_alert_from_outflow(row) for row in outflows)
    report = {
        "scanned_at": now,
        "cached_at": now,
        "partial": partial,
        "readonly": True,
        "note": "Nessuna revoca parte da qui. Il link apre revoke.cash e la firma resta tua, per esempio su OneKey.",
        "approvals": approvals,
        "incidents": incidents,
        "outflows": outflows,
        "alerts": alerts,
        "errors": errors,
        "drop_pct": drop_pct,
        "usd_min": usd_min,
    }
    db.cache_set("security", report)
    db.set_setting("security_scanned_at", str(now))
    return report


def public_report(owner: str = "tutti") -> dict[str, Any]:
    owner = normalize_owner(owner, allow_all=True)
    report = cached_report()
    if not report:
        return {
            "owner": owner,
            "ready": False,
            "running": scan_running(),
            "readonly": True,
            "note": "Nessun controllo ancora. Aprilo e premi Aggiorna, oppure attendi il controllo giornaliero.",
            "approvals": [],
            "incidents": [],
            "outflows": [],
            "alerts": [],
            "errors": [],
            "summary": {"approvals": 0, "hidden": 0, "incidents": 0, "outflows": 0, "visible": 0, "urgent": 0, "urgent_keys": [], "risky_keys": []},
            "counts": {"io": 0, "moglie": 0, "tutti": 0},
        }
    approvals = annotate_risk(
        annotate_hidden(dedupe_approvals(filter_owner_rows(list(report.get("approvals") or []), owner))),
        _portfolio_positions(),
    )
    outflows = filter_owner_rows(list(report.get("outflows") or []), owner)
    alerts = [row for row in filter_owner_rows(list(report.get("alerts") or []), owner) if row.get("kind") != "approval"]
    alerts.extend(item for item in (_alert_from_approval(row) for row in approvals if not row.get("hidden")) if item)
    incidents = list(report.get("incidents") or [])
    if owner != "tutti":
        incidents = [row for row in incidents if owner in (row.get("owners") or [])]
    visible_approvals = _risky_visible(approvals)
    urgent = heavy_urgent_keys(visible_approvals, incidents, outflows)
    summary = {
        "approvals": len(visible_approvals),
        "hidden": sum(1 for row in approvals if row.get("hidden")),
        "incidents": len(incidents),
        "outflows": len(outflows),
        "visible": len(visible_approvals) + len(incidents) + len(outflows),
        "risky_keys": risky_keys(approvals),
        "urgent_keys": urgent,
        "urgent": len(urgent),
    }
    return {
        "owner": owner,
        "ready": True,
        "running": scan_running(),
        "from_cache": True,
        "scanned_at": report.get("scanned_at"),
        "cached_at": report.get("cached_at"),
        "partial": report.get("partial"),
        "readonly": True,
        "note": report.get("note"),
        "approvals": approvals,
        "incidents": incidents,
        "outflows": outflows,
        "alerts": alerts,
        "errors": report.get("errors") or [],
        "drop_pct": report.get("drop_pct"),
        "usd_min": report.get("usd_min"),
        "summary": summary,
        "counts": {
            "io": security_summary("io")["visible"],
            "moglie": security_summary("moglie")["visible"],
            "tutti": security_summary("tutti")["visible"],
        },
    }


def scan_running() -> bool:
    return _SCAN is not None and not _SCAN.done()


def start_scan() -> asyncio.Task:
    global _SCAN
    if scan_running() and _SCAN is not None:
        return _SCAN
    _SCAN = asyncio.create_task(scan_and_store())

    def _finished(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        try:
            exc = task.exception()
        except Exception:
            return
        if exc:
            log.warning("Controllo sicurezza non completato (%s).", type(exc).__name__)

    _SCAN.add_done_callback(_finished)
    return _SCAN


async def daily_loop() -> None:
    """Wait, then refresh at most once a day. Startup does not wait for this."""
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return
    await asyncio.sleep(90)
    while True:
        try:
            last = int(db.get_setting("security_scanned_at") or "0")
        except ValueError:
            last = 0
        if time.time() - last >= CACHE_TTL and not scan_running():
            try:
                await start_scan()
            except Exception as exc:  # noqa: BLE001
                log.warning("Controllo sicurezza non completato (%s).", type(exc).__name__)
        await asyncio.sleep(3600)
