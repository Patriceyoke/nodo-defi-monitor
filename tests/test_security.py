"""Security checks stay read-only: approvals, incidents, outflows."""
from __future__ import annotations

import time

from app.security import (
    aave_frozen_paused,
    annotate_hidden,
    approval_flags,
    approval_severity,
    compare_balance,
    dedupe_approvals,
    is_unlimited,
    latest_approvals,
    match_hacks,
    outflow_rows,
    parse_approval_log,
    public_report,
    revoke_url,
    risk_usd,
    security_summary,
)

WALLET = "0x" + "11" * 20
SPENDER = "0x" + "22" * 20
TOKEN = "0x" + "33" * 20
NOW = 1_700_000_000


def test_unlimited_allowance_is_capped_by_the_balance():
    raw = 2**256 - 1
    assert is_unlimited(raw)
    # 100 tokens of 6 decimals at $1, unlimited allowance.
    assert risk_usd(raw, 100 * 10**6, 6, 1) == 100
    assert risk_usd(40 * 10**6, 100 * 10**6, 6, 2) == 80


def test_approval_flags_and_revoke_link():
    flags = approval_flags(
        unlimited=True, code_empty=True, verified=None, named=False,
        approved_at=NOW - 200 * 86400, now=NOW, exploited=False,
    )
    assert "unlimited" in flags and "eoa" in flags and "stale" in flags
    assert approval_severity(flags) == "danger"
    assert approval_severity(["stale"]) == "warning"
    assert approval_severity(approval_flags(
        unlimited=False, code_empty=False, verified=True, named=True,
        approved_at=NOW, now=NOW, exploited=True,
    )) == "danger"
    assert revoke_url(WALLET, 1) == f"https://revoke.cash/address/{WALLET}?chainId=1"
    assert "revoke.cash" in revoke_url(WALLET, 8453)


def test_latest_log_wins_and_a_revocation_drops_the_approval():
    def log(block: int, amount: int):
        return {
            "address": TOKEN,
            "topics": [
                "0x8c5be1e5ebec7d5bd14f71427d1e84f3dd0314c0f7b2291e5b200ac8c7c3b925",
                "0x" + WALLET[2:].rjust(64, "0"),
                "0x" + SPENDER[2:].rjust(64, "0"),
            ],
            "data": hex(amount),
            "blockNumber": hex(block),
            "timeStamp": hex(NOW - 10 * 86400),
            "transactionHash": "0x" + "ab" * 32,
        }
    active = latest_approvals([log(1, 5), log(2, 0)])
    assert active == []
    kept = latest_approvals([log(3, 2**256 - 1)])
    assert len(kept) == 1 and kept[0]["active"]
    parsed = parse_approval_log(log(3, 2**256 - 1))
    assert parsed["spender"] == SPENDER
    assert is_unlimited(parsed["amount"])


def test_hacks_only_for_protocols_you_hold():
    hacks = [
        {"name": "Aave", "date": NOW - 5 * 86400, "technique": "oracle", "amount": 10},
        {"name": "Some Other Protocol", "date": NOW - 5 * 86400, "technique": "key", "amount": 10},
        {"name": "Aave", "date": NOW - 200 * 86400, "technique": "old", "amount": 1},
    ]
    recent = match_hacks({"Aave"}, hacks, NOW)
    assert len(recent) == 1
    assert recent[0]["severity"] == "danger"
    assert match_hacks({"Compound"}, hacks, NOW) == []
    older = match_hacks({"Aave"}, [{"name": "Aave V3", "date": NOW - 100 * 86400, "technique": "pause"}], NOW)
    assert older[0]["severity"] == "warning"
    frozen, paused = aave_frozen_paused((1 << 57) | (1 << 60))
    assert frozen and paused
    assert aave_frozen_paused(0) == (False, False)


def test_outflow_ignores_the_first_reading_and_small_moves():
    assert compare_balance(None, 10, 100, 5, 500) is None
    assert compare_balance({"amount": 10, "usd": 1000}, 9.9, 990, 5, 500) is None
    assert compare_balance({"amount": 10, "usd": 1000}, 9, 900, 5, 500) == "percent"
    assert "usd" in compare_balance({"amount": 10, "usd": 5000}, 8, 4000, 5, 500)
    positions = [{
        "address": WALLET, "chain": "ethereum", "token": TOKEN, "owner": "io",
        "label": "Casa", "asset": "USDC", "amount": 9, "usd": 900,
    }]
    alerts, updates = outflow_rows(positions, [], {WALLET.lower()}, 5, 500)
    assert alerts == [] and updates[0]["amount"] == 9
    marks = [{"wallet": WALLET, "chain": "ethereum", "token": TOKEN, "owner": "io", "symbol": "USDC", "amount": 10, "usd": 5000}]
    alerts, _updates = outflow_rows(positions, marks, {WALLET.lower()}, 5, 500)
    assert alerts[0]["owner"] == "io"
    assert alerts[0]["severity"] == "danger"
    quiet, _updates = outflow_rows(
        [{**positions[0], "owner": "moglie"}], marks, {WALLET.lower()}, 5, 500,
    )
    assert quiet[0]["owner"] == "moglie"


def test_same_spender_is_shown_once_and_a_changed_allowance_returns():
    first = {"wallet": WALLET, "chain": "ethereum", "token": TOKEN, "spender": SPENDER, "kind": "erc20", "severity": "warning", "allowance": "10", "approved_at": 1, "symbol": "cWETHv3", "owner": "io"}
    second = {**first, "kind": "erc721", "severity": "danger", "allowance": "unlimited", "approved_at": 2, "symbol": "aEthWETH"}
    rows = dedupe_approvals([first, second, {**first, "wallet": "0x" + "44" * 20}])
    assert len(rows) == 2
    kept = [row for row in rows if row["wallet"] == WALLET][0]
    assert kept["allowance"] == "unlimited"
    hidden = annotate_hidden(rows, [{"wallet": WALLET.lower(), "chain": "ethereum", "token": TOKEN.lower(), "spender": SPENDER.lower(), "allowance": "unlimited"}])
    by_wallet = {row["wallet"].lower(): row for row in hidden}
    assert by_wallet[WALLET.lower()]["hidden"] is True
    changed = annotate_hidden([{**kept, "allowance": "12"}], [{"wallet": WALLET.lower(), "chain": "ethereum", "token": TOKEN.lower(), "spender": SPENDER.lower(), "allowance": "unlimited"}])
    assert changed[0]["hidden"] is False


def test_hidden_approval_stays_out_of_the_counter(tmp_path, monkeypatch):
    from app import config, db

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "monitor.db")
    monkeypatch.setattr(config, "_READY", True)
    monkeypatch.setattr(config, "DATA_PERSISTENT", True)
    db.init_db()
    db.add_evm("Casa", WALLET, "moglie")
    db.cache_set("security", {
        "scanned_at": 1,
        "approvals": [
            {"owner": "io", "wallet": WALLET, "chain": "ethereum", "token": TOKEN, "spender": SPENDER, "allowance": "unlimited", "severity": "danger", "symbol": "cWETHv3"},
            {"owner": "io", "wallet": WALLET, "chain": "ethereum", "token": TOKEN, "spender": SPENDER, "allowance": "5", "severity": "warning", "symbol": "cWETHv3", "kind": "erc721"},
        ],
        "incidents": [],
        "outflows": [{"owner": "moglie", "symbol": "ETH", "severity": "warning", "wallet": WALLET, "chain": "ethereum"}],
        "alerts": [],
        "errors": [],
    })
    assert security_summary("io")["approvals"] == 1
    assert security_summary("io")["visible"] == 1
    db.hide_approval(WALLET, "ethereum", TOKEN, SPENDER, "unlimited")
    assert security_summary("io")["approvals"] == 0
    assert security_summary("io")["hidden"] == 1
    assert security_summary("tutti")["outflows"] == 1
    db.init_db()
    assert db.list_evm()[0]["address"] == WALLET
    assert len(db.list_approval_hides()) == 1


def test_cached_security_does_not_scan_and_wallets_survive(tmp_path, monkeypatch):
    from app import config, db

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "monitor.db")
    monkeypatch.setattr(config, "_READY", True)
    monkeypatch.setattr(config, "DATA_PERSISTENT", True)
    db.init_db()
    db.add_evm("Casa", WALLET, "io")
    db.cache_set("security", {
        "scanned_at": int(time.time()),
        "readonly": True,
        "note": "firma tua",
        "approvals": [{"owner": "io", "symbol": "USDC", "severity": "warning", "flags": ["stale"], "revoke_url": revoke_url(WALLET, 1)}],
        "incidents": [{"protocol": "Aave", "owners": ["io"], "severity": "warning", "detail": "test"}],
        "outflows": [{"owner": "moglie", "symbol": "ETH", "severity": "danger"}],
        "alerts": [
            {"owner": "io", "level": "warning", "kind": "approval", "title": "USDC", "text": "stale"},
            {"owner": "moglie", "level": "danger", "kind": "outflow", "title": "ETH", "text": "uscita"},
        ],
        "errors": [],
    })

    async def explode():
        raise AssertionError("la cache non deve rifare il controllo")

    monkeypatch.setattr("app.security.scan_and_store", explode)
    view = public_report("io")
    assert view["ready"] is True
    assert len(view["approvals"]) == 1
    assert view["outflows"] == []
    assert view["counts"]["moglie"] == 1
    assert "revoke.cash" in view["approvals"][0]["revoke_url"]
    db.init_db()
    assert db.list_evm()[0]["address"] == WALLET
