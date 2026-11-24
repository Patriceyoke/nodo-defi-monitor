"""0.6.4: empty chains are not a partial scan, and a stale zero balance is not urgent."""
from __future__ import annotations

import asyncio

from app.security import classify_approval

WALLET = "0x" + "11" * 20
OTHER = "0x" + "44" * 20
SPENDER = "0x" + "22" * 20
TOKEN = "0x" + "33" * 20
WSTETH = "0x" + "55" * 20


def _isolate(monkeypatch, tmp_path):
    from app import config, db

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "monitor.db")
    monkeypatch.setattr(config, "_READY", True)
    monkeypatch.setattr(config, "DATA_PERSISTENT", True)
    db.init_db()
    return db


def _approval(symbol: str, token: str, **extra):
    row = {
        "owner": "io",
        "label": "Casa",
        "wallet": WALLET,
        "chain": "ethereum",
        "token": token,
        "spender": SPENDER,
        "symbol": symbol,
        "allowance": "unlimited",
        "flags": ["unlimited", "unknown", "stale"],
        "severity": "danger",
        "hidden": False,
        "value_at_risk_usd": 0,
        "risk_class": "rischiosa",
        "risk_reason": "Permesso ampio verso un contratto che non è un protocollo che stai usando.",
    }
    row.update(extra)
    return row


def test_position_tokens_stay_ok_and_zero_stale_is_not_danger():
    positions = [
        {"owner": "io", "protocol": "Aave", "asset": "USDC", "usd": 10, "amount": 1},
        {"owner": "io", "protocol": "Compound", "asset": "USDC", "usd": 10, "amount": 1},
        {"owner": "io", "protocol": "Lido", "asset": "wstETH", "usd": 10, "amount": 1},
    ]
    for symbol in ("aEthUSDC", "aEthUSDT", "aEthWETH", "cUSDCv3", "cUSDTv3", "cWETHv3"):
        kind, reason = classify_approval(
            {"owner": "io", "symbol": symbol, "flags": ["unlimited", "unknown"], "allowance": "unlimited", "value_at_risk_usd": 0},
            positions,
        )
        assert kind == "normale", symbol
        assert "Token della posizione" in reason
    kind, reason = classify_approval(_approval("token", TOKEN), positions)
    assert kind == "normale"
    assert reason == "permesso vecchio, saldo zero"
    kind, reason = classify_approval(
        _approval("USDC", TOKEN, flags=["unlimited", "unknown"], value_at_risk_usd=800, spender=SPENDER),
        positions,
    )
    assert kind == "rischiosa"
    assert "non è un protocollo" in reason


def test_cached_empty_chains_and_zero_balances_are_rewritten(tmp_path, monkeypatch):
    from app import security

    db = _isolate(monkeypatch, tmp_path)
    db.add_evm("Casa", WALLET, "io")
    db.cache_set("portfolio", {"positions": [{
        "address": WALLET, "chain": "ethereum", "protocol": "Aave", "asset": "USDC",
        "amount": 2, "usd": 20, "owner": "io",
    }]})
    db.cache_set("security", {
        "scanned_at": 50,
        "cached_at": 50,
        "from_cache": False,
        "partial": True,
        "running": False,
        "usd_min": 500,
        "note": "vecchia",
        "approvals": [
            _approval("token", TOKEN),
            _approval("wstETH", WSTETH, spender=OTHER),
            _approval("USDC", "0x" + "66" * 20, flags=["unlimited", "unknown"], value_at_risk_usd=800, spender="0x" + "77" * 20),
            _approval("DAI", "0x" + "88" * 20, flags=["unlimited", "unknown"], value_at_risk_usd=100, spender="0x" + "99" * 20),
        ],
        "incidents": [
            {"protocol": "Fluid", "name": "Fluidkey / Database Breach", "owners": ["io"], "severity": "danger", "detail": "exploit"},
            {"protocol": "Lido", "name": "Lido Earn: Introducing Two Layers of Loss Protection", "owners": ["io"], "severity": "danger", "detail": "comunicazione ufficiale"},
        ],
        "outflows": [],
        "alerts": [
            {"owner": "io", "level": "danger", "kind": "approval", "title": "Revoca token", "text": "Revoca"},
            {"owner": "io", "level": "danger", "kind": "approval", "title": "Revoca wstETH", "text": "Revoca"},
            {"owner": "io", "level": "danger", "kind": "incident", "title": "Protocollo Fluid", "text": "Fluidkey / Database Breach"},
        ],
        "errors": [
            "Approvazioni su polygon non lette: la lettura dei log si è interrotta.",
            "Approvazioni su arbitrum non lette: la lettura dei log si è interrotta.",
            "Approvazioni su base non lette: la lettura dei log si è interrotta.",
        ],
    })
    security.migrate_security_cache()
    view = security.public_report("tutti")
    text = str(view)
    assert view["running"] is False
    assert view["partial"] is False
    assert view["scanned_at"] == 50
    assert "Fluidkey" not in text
    assert "comunicazione ufficiale" not in text
    assert "Protocollo Fluid" not in text
    joined = " ".join(view["errors"])
    assert "polygon" not in joined and "arbitrum" not in joined and "base" not in joined
    assert "polygon" not in view["note"] and "arbitrum" not in view["note"] and "base" not in view["note"]
    urgent = " ".join(view["summary"]["urgent_keys"])
    assert TOKEN not in urgent and WSTETH not in urgent and OTHER not in urgent
    hidden = [row for row in view["approvals"] if row.get("hidden")]
    assert len(hidden) == 2
    assert all(row["severity"] == "ok" for row in hidden)
    assert all(row["risk_reason"] == "permesso vecchio, saldo zero" for row in hidden)
    assert all(row["risk_class"] != "rischiosa" for row in hidden)
    titles = [row.get("title") for row in view["alerts"]]
    assert "Revoca token" not in titles
    assert "Revoca wstETH" not in titles
    live = next(row for row in view["approvals"] if row["symbol"] == "USDC")
    assert live["hidden"] is not True
    assert live["severity"] == "danger"
    assert live["risk_class"] == "rischiosa"
    assert any(live["token"] in key for key in view["summary"]["urgent_keys"])
    small = next(row for row in view["approvals"] if row["symbol"] == "DAI")
    assert small["severity"] == "danger"
    assert small["hidden"] is not True
    assert all(small["token"] not in key for key in view["summary"]["urgent_keys"])
    stored = db.cache_get("security")
    stored_errors = " ".join(stored["errors"])
    assert "polygon" not in stored_errors
    assert stored["partial"] is False


def test_scan_skips_empty_chains_and_retries_a_short_window(tmp_path, monkeypatch):
    from app import security

    db = _isolate(monkeypatch, tmp_path)
    db.add_evm("Casa", WALLET, "io")
    db.cache_set("portfolio", {"positions": [{
        "address": WALLET, "chain": "ethereum", "protocol": "Aave", "asset": "USDC",
        "amount": 1, "usd": 5, "owner": "io",
    }]})
    calls: list[tuple[str, int]] = []

    async def logs(chain, wallet, chunk_blocks=security.CHUNK_BLOCKS):
        calls.append((chain, chunk_blocks))
        if chunk_blocks >= security.CHUNK_BLOCKS:
            return [], True
        return [], False

    async def empty(*_args, **_kwargs):
        return []

    monkeypatch.setattr(security, "_approval_logs", logs)
    monkeypatch.setattr(security, "_hacks", empty)
    monkeypatch.setattr(security, "_official_notes", empty)
    monkeypatch.setattr(security, "_pause_flags", empty)
    report = asyncio.run(security.scan_and_store())
    assert report["partial"] is False
    assert report["from_cache"] is False
    assert report["errors"] == []
    assert "polygon" not in report["note"]
    chains = {chain for chain, _size in calls}
    assert chains == {"ethereum"}
    sizes = [size for chain, size in calls if chain == "ethereum"]
    assert sizes[0] == security.CHUNK_BLOCKS
    assert sizes[1] < security.CHUNK_BLOCKS
