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


def _worth_showing(delta: float, usd_delta: float, usd_min: float) -> bool:
    """Hide a quantity that would print as 0, unless the dollars are already meaningful."""
    if abs(float(delta)) >= 0.005:
        return True
    return float(usd_delta) >= float(usd_min) and float(usd_delta) > 0


def observe_positions(positions: list[dict[str, Any]], *, now: int | None = None) -> list[dict[str, Any]]:
    """Compare token quantities with the last refresh.

    Interest and rebasing only move the saved quantity forward. They are not
    stored as events. A later decrease is an outflow only when it passes the
    quantity threshold.
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
        db.upsert_position_mark(
            key, current["owner"], current["protocol"], current["asset"],
            current["wallet"], current["chain"], current["amount"], current["usd"],
        )
        if previous is None:
            continue
        drop = meaningful_quantity_drop(
            float(previous["amount"]), float(previous.get("usd") or 0), current["amount"], drop_pct, usd_min,
        )
        if not drop or not _worth_showing(drop["delta"], drop["usd_delta"], usd_min):
            continue
        row = {
            "flow_key": _flow_key("out", key, float(previous["amount"]), current["amount"]),
            "direction": "out",
            "owner": current["owner"],
            "protocol": current["protocol"],
            "asset": current["asset"],
            "wallet": current["wallet"],
            "chain": current["chain"],
            "previous_amount": float(previous["amount"]),
            "amount": current["amount"],
            "delta": drop["delta"],
            "usd_delta": drop["usd_delta"],
            "detected_at": now,
            "status": "open",
        }
        if db.insert_fund_flow(row):
            created.append(row)
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
    visible = []
    for row in rows:
        if row.get("direction") != "out":
            continue
        if not _worth_showing(float(row.get("delta") or 0), float(row.get("usd_delta") or 0), 500):
            continue
        visible.append(public_flow(row))
    return visible
