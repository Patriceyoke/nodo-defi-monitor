"""Short answers for Hermes. Local math on the cached portfolio. No addresses."""
from __future__ import annotations

from typing import Any

REWARD_USD = 5.0
HEALTH_ALERT = 1.6
DEPEG_BAND = 0.02
STABLES = {
    "usdc", "usdt", "dai", "usds", "susds", "stusds", "sdai", "gho",
    "eurc", "rlusd", "usdg", "frxusd", "lusd", "crvusd", "pyusd", "frax",
}


def _num(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _label(row: dict[str, Any]) -> str:
    return str(row.get("label") or "Portafoglio")


def scope_positions(
    positions: list[dict[str, Any]] | None,
    owner: str = "combinato",
    wallet: str | None = None,
) -> list[dict[str, Any]]:
    who = (owner or "combinato").strip().lower()
    if who in {"tutti", "all"}:
        who = "combinato"
    name = (wallet or "").strip()
    chosen: list[dict[str, Any]] = []
    for row in positions or []:
        if who in {"io", "moglie"} and row.get("owner") != who:
            continue
        if name and str(row.get("label") or "") != name:
            continue
        chosen.append(row)
    return chosen


def portfolio_summary(
    positions: list[dict[str, Any]] | None,
    owner: str = "combinato",
    wallet: str | None = None,
) -> dict[str, Any]:
    rows = scope_positions(positions, owner, wallet)
    grouped: dict[tuple[str, str], dict[str, float]] = {}
    for row in rows:
        usd = _num(row.get("usd")) or 0.0
        apy = _num(row.get("apy"))
        key = (str(row.get("protocol") or "Posizione"), str(row.get("asset") or ""))
        slot = grouped.setdefault(key, {"usd": 0.0, "weighted": 0.0, "base": 0.0})
        slot["usd"] += usd
        if apy is not None and usd > 0:
            slot["weighted"] += usd * apy
            slot["base"] += usd
    lines = []
    for (protocol, asset), slot in grouped.items():
        net = round(slot["weighted"] / slot["base"], 2) if slot["base"] else None
        lines.append({
            "protocol": protocol,
            "asset": asset,
            "usd": round(slot["usd"], 2),
            "apy": net,
        })
    lines.sort(key=lambda item: item["usd"], reverse=True)
    shown = lines[:5]
    rest = lines[5:]
    if rest:
        shown.append({
            "protocol": "altro",
            "asset": "",
            "usd": round(sum(item["usd"] for item in rest), 2),
            "apy": None,
            "count": len(rest),
        })
    total = round(sum(item["usd"] for item in lines), 2)
    base = sum(item["usd"] for item in lines if item["apy"] is not None)
    weighted = sum(item["usd"] * item["apy"] for item in lines if item["apy"] is not None)
    text_lines = [f"{owner} · totale {total} USD"]
    if wallet:
        text_lines[0] = f"{wallet} · {text_lines[0]}"
    for item in shown:
        apy = "n/d" if item["apy"] is None else f"{item['apy']}%"
        name = item["protocol"] if item["protocol"] == "altro" else f"{item['protocol']} {item['asset']}".strip()
        text_lines.append(f"{name} · {item['usd']} USD · APY {apy}")
    return {
        "owner": owner,
        "wallet": wallet or None,
        "total_usd": total,
        "apy": round(weighted / base, 2) if base else None,
        "lines": shown,
        "text": "\n".join(text_lines),
    }


def _depeg(row: dict[str, Any]) -> str | None:
    if row.get("depeg"):
        return f"{row.get('protocol') or ''} {row.get('asset') or ''} segnato come depeg".strip()
    asset = str(row.get("asset") or "").lower()
    price = _num(row.get("price_usd"))
    if asset in STABLES and price is not None and abs(price - 1) > DEPEG_BAND:
        return f"{row.get('asset')} a {price} USD, lontano da 1"
    return None


def risk_and_alerts(
    positions: list[dict[str, Any]] | None,
    errors: list[Any] | None = None,
) -> dict[str, Any]:
    items: list[dict[str, str]] = []
    for row in positions or []:
        label = _label(row)
        health = _num(row.get("health_factor"))
        if health is not None and health < HEALTH_ALERT:
            items.append({
                "kind": "health",
                "text": f"{label} · {row.get('protocol') or ''} health factor {health}",
            })
        reward = _num(row.get("claimable_usd"))
        if reward is None:
            reward = _num(row.get("rewards_usd"))
        if reward is not None and reward >= REWARD_USD:
            items.append({
                "kind": "reward",
                "text": f"{label} · {row.get('protocol') or ''} da ritirare {round(reward, 2)} USD",
            })
        note = _depeg(row)
        if note:
            items.append({"kind": "depeg", "text": f"{label} · {note}"})
    for raw in errors or []:
        text = str(raw or "").strip()
        if not text:
            continue
        lowered = text.lower()
        kind = "depeg" if "depeg" in lowered else "error"
        items.append({"kind": kind, "text": text})
    if not items:
        return {"text": "Nessun rischio", "items": []}
    return {"text": "\n".join(item["text"] for item in items), "items": items}


def simulate_market_shock(
    positions: list[dict[str, Any]] | None,
    asset: str,
    pct_change: float,
) -> dict[str, Any]:
    """Scale matching values. Health moves with collateral, or with the debt asset."""
    factor = 1.0 + float(pct_change) / 100.0
    target = str(asset or "").strip().lower()
    before = 0.0
    after = 0.0
    rows: list[dict[str, Any]] = []
    for raw in positions or []:
        usd = _num(raw.get("usd")) or 0.0
        before += usd
        moved = str(raw.get("asset") or "").strip().lower() == target
        new_usd = usd * factor if moved else usd
        after += new_usd
        health = _num(raw.get("health_factor"))
        debt = _num(raw.get("debt_usd"))
        collateral = _num(raw.get("collateral_usd"))
        debt_asset = str(raw.get("debt_asset") or "").strip().lower()
        new_health = health
        room = None
        if health is not None and debt and debt > 0:
            base = collateral if collateral is not None else usd
            new_collateral = base * factor if moved else base
            new_debt = debt * factor if debt_asset == target else debt
            if base > 0 and new_debt > 0:
                new_health = health * (new_collateral / base) / (new_debt / debt)
            if new_health and new_health > 0:
                room = (1 - (1 / new_health)) * 100
        rows.append({
            "label": _label(raw),
            "protocol": raw.get("protocol"),
            "asset": raw.get("asset"),
            "usd_before": round(usd, 2),
            "usd_after": round(new_usd, 2),
            "health_factor_before": health,
            "health_factor_after": None if new_health is None else round(new_health, 4),
            "liquidation_room_pct": None if room is None else round(room, 2),
        })
    return {
        "asset": asset,
        "pct_change": float(pct_change),
        "value_before_usd": round(before, 2),
        "value_after_usd": round(after, 2),
        "value_delta_usd": round(after - before, 2),
        "positions": rows,
        "note": "Calcolo locale sul saldo già in memoria. Nessuna lettura di rete.",
    }


def without_addresses(payload: Any) -> Any:
    """Drop address-shaped fields before a tool answer leaves the process."""
    if isinstance(payload, dict):
        clean = {}
        for key, value in payload.items():
            if key in {"address", "wallet_address", "spender", "token"}:
                continue
            if isinstance(value, str) and value.startswith("0x") and len(value) >= 42:
                continue
            clean[key] = without_addresses(value)
        return clean
    if isinstance(payload, list):
        return [without_addresses(item) for item in payload]
    if isinstance(payload, str) and payload.startswith("0x") and len(payload) >= 42:
        return ""
    return payload
