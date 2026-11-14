"""Quantity changes between refreshes. Price moves do not count as money leaving."""
from __future__ import annotations

import time
from typing import Any

from . import db

_DUST = 1e-8


def position_key(row: dict[str, Any]) -> str:
    return "|".join(
        (
            str(row.get("owner") or ""),
            str(row.get("protocol") or ""),
            str(row.get("asset") or ""),
            str(row.get("chain") or "ethereum"),
            str(row.get("address") or row.get("wallet") or "").lower(),
        )
    ).lower()


def _thresholds() -> tuple[float, float]:
    try:
        drop = float(db.get_setting("outflow_drop_pct") or "2")
    except ValueError:
        drop = 2.0
    try:
        usd = float(db.get_setting("outflow_usd") or "500")
    except ValueError:
        usd = 500.0
    return max(0.1, min(drop, 100.0)), max(0.0, usd)


def meaningful_quantity_drop(previous_amount: float, previous_usd: float, amount: float, drop_pct: float, usd_min: float) -> dict[str, float] | None:
    """A drop in token quantity. The dollar test uses the previous price, not today's."""
    previous_amount = float(previous_amount or 0)
    amount = float(amount or 0)
    if previous_amount <= 0:
        return None
    delta = previous_amount - amount
    if delta <= max(previous_amount * _DUST, 1e-9):
        return None
    frac = delta / previous_amount
    usd_delta = frac * float(previous_usd or 0)
    if frac * 100 < float(drop_pct) and usd_delta < float(usd_min):
        return None
    return {"delta": delta, "usd_delta": usd_delta, "frac": frac}


def _flow_key(direction: str, key: str, previous: float, amount: float) -> str:
    return f"{direction}|{key}|{previous:.8f}|{amount:.8f}"


def observe_positions(positions: list[dict[str, Any]], *, now: int | None = None) -> list[dict[str, Any]]:
    """Compare token quantities with the last refresh and store marks plus events.

    The first time a position is seen, only the mark is stored. A later increase
    is an inflow and stays out of the banner. A meaningful decrease is an outflow.
    """
    now = int(time.time() if now is None else now)
    drop_pct, usd_min = _thresholds()
    known = {row["position_key"]: row for row in db.list_position_marks()}
    grouped: dict[str, dict[str, Any]] = {}
    for row in positions:
        key = position_key(row)
        if not key.strip("|"):
            continue
        bucket = grouped.get(key)
        amount = float(row.get("amount") or 0)
        usd = float(row.get("usd") or 0)
        if bucket is None:
            grouped[key] = {
                "position_key": key,
                "owner": str(row.get("owner") or ""),
                "protocol": str(row.get("protocol") or ""),
                "asset": str(row.get("asset") or ""),
                "wallet": str(row.get("address") or row.get("wallet") or "").lower(),
                "chain": str(row.get("chain") or "ethereum"),
                "amount": amount,
                "usd": usd,
            }
        else:
            bucket["amount"] += amount
            bucket["usd"] += usd
    created: list[dict[str, Any]] = []
    for key, current in grouped.items():
        previous = known.get(key)
        if previous is None:
            db.upsert_position_mark(
                key, current["owner"], current["protocol"], current["asset"],
                current["wallet"], current["chain"], current["amount"], current["usd"],
            )
            continue
        prev_amount = float(previous["amount"])
        if current["amount"] > prev_amount + max(prev_amount * _DUST, 1e-9):
            delta = current["amount"] - prev_amount
            frac = delta / prev_amount if prev_amount else 0
            row = {
                "flow_key": _flow_key("in", key, prev_amount, current["amount"]),
                "direction": "in",
                "owner": current["owner"],
                "protocol": current["protocol"],
                "asset": current["asset"],
                "wallet": current["wallet"],
                "chain": current["chain"],
                "previous_amount": prev_amount,
                "amount": current["amount"],
                "delta": delta,
                "usd_delta": frac * float(previous.get("usd") or 0),
                "detected_at": now,
                "status": "noted",
            }
            if db.insert_fund_flow(row):
                created.append(row)
        else:
            drop = meaningful_quantity_drop(prev_amount, float(previous.get("usd") or 0), current["amount"], drop_pct, usd_min)
            if drop:
                row = {
                    "flow_key": _flow_key("out", key, prev_amount, current["amount"]),
                    "direction": "out",
                    "owner": current["owner"],
                    "protocol": current["protocol"],
                    "asset": current["asset"],
                    "wallet": current["wallet"],
                    "chain": current["chain"],
                    "previous_amount": prev_amount,
                    "amount": current["amount"],
                    "delta": drop["delta"],
                    "usd_delta": drop["usd_delta"],
                    "detected_at": now,
                    "status": "open",
                }
                if db.insert_fund_flow(row):
                    created.append(row)
        db.upsert_position_mark(
            key, current["owner"], current["protocol"], current["asset"],
            current["wallet"], current["chain"], current["amount"], current["usd"],
        )
    return created


def public_flow(row: dict[str, Any]) -> dict[str, Any]:
    """Fields safe to show. The wallet address stays in the database only."""
    status = str(row.get("status") or "open")
    direction = str(row.get("direction") or "out")
    urgent = direction == "out" and status in {"open", "suspicious"}
    return {
        "id": row.get("id"),
        "direction": direction,
        "owner": row.get("owner"),
        "protocol": row.get("protocol"),
        "asset": row.get("asset"),
        "delta": row.get("delta"),
        "usd_delta": row.get("usd_delta"),
        "detected_at": row.get("detected_at"),
        "status": status,
        "confirmation": status,
        "urgent": urgent,
    }


def list_public_flows(owner: str = "tutti") -> list[dict[str, Any]]:
    rows = db.list_fund_flows()
    if owner and owner != "tutti":
        rows = [row for row in rows if row.get("owner") == owner]
    return [public_flow(row) for row in rows]
