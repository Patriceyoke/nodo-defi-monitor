"""Per-wallet value history and estimated interest from hourly snapshots.

Interest is the growth in token amount, valued at the latest price. A jump much
larger than the APY would accrue is treated as a deposit or a withdrawal.
A price move with the same amount is not interest. Months use Asia/Bangkok.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

BANGKOK = ZoneInfo("Asia/Bangkok")
YEAR_SECONDS = 365.25 * 86400
NOTE = (
    "Stima. Conta solo la crescita della quantità tra uno snapshot e l'altro, "
    "valutata al prezzo di adesso. Un versamento o un prelievo molto più grande "
    "dell'interesse atteso non conta. Il solo movimento di prezzo non conta."
)


def empty_realized_yield() -> dict[str, Any]:
    return {
        "estimate": True,
        "note": NOTE,
        "current_month_usd": 0.0,
        "months": [],
        "by_wallet": [],
    }


def empty_wallet_series() -> dict[str, Any]:
    return {"wallets": [], "points": []}


def _wallet_id(owner: str, label: str) -> str:
    return f"{owner}::{label}"


def _num(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _position_key(row: dict[str, Any]) -> tuple[str, str, str, str, str]:
    return (
        str(row.get("owner") or ""),
        str(row.get("label") or "Portafoglio"),
        str(row.get("chain") or ""),
        str(row.get("protocol") or ""),
        str(row.get("asset") or ""),
    )


def _index(positions: list[dict[str, Any]] | None) -> dict[tuple[str, str, str, str, str], dict[str, float | None]]:
    out: dict[tuple[str, str, str, str, str], dict[str, float | None]] = {}
    for row in positions or []:
        if not isinstance(row, dict):
            continue
        key = _position_key(row)
        slot = out.setdefault(key, {"amount": 0.0, "value": 0.0, "apy_weight": 0.0, "apy_base": 0.0, "apy": None})
        amount = _num(row.get("amount")) or 0.0
        value = _num(row.get("value_usd"))
        if value is None:
            value = _num(row.get("usd")) or 0.0
        slot["amount"] = float(slot["amount"] or 0) + amount
        slot["value"] = float(slot["value"] or 0) + value
        apy = _num(row.get("apy"))
        if apy is not None and amount > 0:
            slot["apy_weight"] = float(slot["apy_weight"] or 0) + apy * amount
            slot["apy_base"] = float(slot["apy_base"] or 0) + amount
    for slot in out.values():
        base = float(slot["apy_base"] or 0)
        slot["apy"] = (float(slot["apy_weight"] or 0) / base) if base else None
    return out


def _price(slot: dict[str, float | None] | None) -> float | None:
    if not slot:
        return None
    amount = float(slot.get("amount") or 0)
    value = float(slot.get("value") or 0)
    if amount <= 0 or value <= 0:
        return None
    return value / amount


def accrual_amount(previous: float, current: float, apy: float | None, seconds: float) -> float:
    """Token amount that looks like interest, not a deposit or a withdrawal."""
    if previous <= 0 or seconds <= 0 or current <= previous or apy is None:
        return 0.0
    rate = float(apy) / 100.0
    if rate <= 0:
        return 0.0
    expected = previous * ((1.0 + rate) ** (seconds / YEAR_SECONDS) - 1.0)
    delta = current - previous
    if expected <= 0 or delta > expected * 2.5:
        return 0.0
    return delta


def _month_key(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), BANGKOK).strftime("%Y-%m")


def _wallets_from_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    wallets = []
    for row in payload.get("wallets") or []:
        if not isinstance(row, dict):
            continue
        label = str(row.get("label") or "Portafoglio")
        owner = str(row.get("owner") or "")
        wallets.append({
            "id": _wallet_id(owner, label),
            "label": label,
            "owner": owner,
            "total_usd": round(float(_num(row.get("total_usd")) or 0), 2),
        })
    if wallets:
        return wallets
    return []


def build(rows: list[dict[str, Any]] | None, *, now: int | None = None) -> dict[str, Any]:
    """Value series plus estimated interest. ``rows`` are history payloads with ts."""
    ordered = sorted(
        [row for row in (rows or []) if isinstance(row, dict) and row.get("ts") is not None],
        key=lambda row: int(row["ts"]),
    )
    points: list[dict[str, Any]] = []
    seen_wallets: dict[str, dict[str, str]] = {}
    indexed: list[tuple[int, dict[tuple[str, str, str, str, str], dict[str, float | None]]]] = []
    for row in ordered:
        payload = row
        total = _num(payload.get("total_usd"))
        if total is None:
            total = _num(payload.get("defi_usd")) or 0.0
        wallets = _wallets_from_payload(payload)
        for wallet in wallets:
            seen_wallets[wallet["id"]] = {"id": wallet["id"], "label": wallet["label"], "owner": wallet["owner"]}
        points.append({
            "ts": int(row["ts"]),
            "total_usd": round(float(total or 0), 2),
            "wallets": wallets,
        })
        indexed.append((int(row["ts"]), _index(payload.get("positions"))))

    latest_prices: dict[tuple[str, str, str, str, str], float] = {}
    for _ts, slots in reversed(indexed):
        for key, slot in slots.items():
            if key in latest_prices:
                continue
            price = _price(slot)
            if price is not None:
                latest_prices[key] = price

    interest: dict[tuple[str, str], dict[str, float]] = {}
    for (prev_ts, prev_slots), (next_ts, next_slots) in zip(indexed, indexed[1:]):
        seconds = next_ts - prev_ts
        if seconds <= 0:
            continue
        month = _month_key(next_ts)
        for key, prev in prev_slots.items():
            nxt = next_slots.get(key)
            if not nxt:
                continue
            gained = accrual_amount(
                float(prev.get("amount") or 0),
                float(nxt.get("amount") or 0),
                prev.get("apy") if isinstance(prev.get("apy"), (int, float)) else None,
                seconds,
            )
            if gained <= 0:
                continue
            price = latest_prices.get(key) or _price(nxt) or _price(prev)
            if not price:
                continue
            owner, label = key[0], key[1]
            bucket = interest.setdefault((owner, label), {})
            bucket[month] = bucket.get(month, 0.0) + gained * price

    moment = int(datetime.now(BANGKOK).timestamp() if now is None else now)
    current_month = _month_key(moment)
    by_wallet = []
    month_totals: dict[str, float] = {}
    for (owner, label), months in sorted(interest.items()):
        packed = [{"month": month, "usd": round(usd, 2)} for month, usd in sorted(months.items())]
        for item in packed:
            month_totals[item["month"]] = month_totals.get(item["month"], 0.0) + item["usd"]
        by_wallet.append({
            "id": _wallet_id(owner, label),
            "label": label,
            "owner": owner,
            "current_month_usd": round(months.get(current_month, 0.0), 2),
            "months": packed,
        })
    months_out = [{"month": month, "usd": round(usd, 2)} for month, usd in sorted(month_totals.items())]
    return {
        "wallet_series": {
            "wallets": list(seen_wallets.values()),
            "points": points,
        },
        "realized_yield": {
            "estimate": True,
            "note": NOTE,
            "current_month_usd": round(month_totals.get(current_month, 0.0), 2),
            "months": months_out,
            "by_wallet": by_wallet,
        },
    }
