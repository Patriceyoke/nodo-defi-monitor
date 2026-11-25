"""Hourly portfolio rows built from positions already in memory.

Nothing here talks to a chain. Labels only: addresses stay out of the row.
"""
from __future__ import annotations

from typing import Any


def _usd(row: dict[str, Any]) -> float:
    try:
        return float(row.get("usd") or 0)
    except (TypeError, ValueError):
        return 0.0


def _apy(row: dict[str, Any]) -> float | None:
    value = row.get("apy")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _health(row: dict[str, Any]) -> float | None:
    value = row.get("health_factor")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _allocation(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], float, float | None, float | None]:
    grouped: dict[tuple[str, str], float] = {}
    weighted = 0.0
    weighted_usd = 0.0
    health: list[float] = []
    total = 0.0
    for row in rows:
        usd = _usd(row)
        total += usd
        key = (str(row.get("protocol") or "Posizione"), str(row.get("asset") or ""))
        grouped[key] = grouped.get(key, 0.0) + usd
        apy = _apy(row)
        if apy is not None and usd > 0:
            weighted += usd * apy
            weighted_usd += usd
        factor = _health(row)
        if factor is not None:
            health.append(factor)
    allocation = [
        {
            "protocol": protocol,
            "asset": asset,
            "usd": round(usd, 2),
            "share": round(usd / total, 6) if total else 0,
        }
        for (protocol, asset), usd in sorted(grouped.items(), key=lambda item: item[1], reverse=True)
    ]
    average = round(weighted / weighted_usd, 4) if weighted_usd else None
    minimum = min(health) if health else None
    return allocation, round(total, 2), average, minimum


def snapshot_rows(positions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One aggregate for Io, Moglie and Combinato, plus one row per wallet label."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {
        ("io", ""): [],
        ("moglie", ""): [],
        ("combinato", ""): [],
    }
    for row in positions or []:
        owner = str(row.get("owner") or "")
        if owner not in {"io", "moglie"}:
            continue
        groups[(owner, "")].append(row)
        groups[("combinato", "")].append(row)
        label = str(row.get("label") or "").strip()
        if label:
            groups.setdefault((owner, label), []).append(row)
    out: list[dict[str, Any]] = []
    for (owner, wallet), rows in groups.items():
        if wallet and not rows:
            continue
        allocation, total, apy, health = _allocation(rows)
        out.append({
            "owner": owner,
            "wallet": wallet,
            "net_worth_usd": total,
            "allocation": allocation,
            "apy": apy,
            "health_factor": health,
        })
    return out
