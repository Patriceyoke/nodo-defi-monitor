"""Local SQLite storage. Wallets, settings and history stay on disk only."""
from __future__ import annotations

import json
import secrets
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Iterator

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evm_wallets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    label TEXT NOT NULL,
    address TEXT NOT NULL,
    owner TEXT NOT NULL CHECK (owner IN ('io', 'moglie')),
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS btc_wallets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    label TEXT NOT NULL,
    kind TEXT NOT NULL,
    descriptor TEXT NOT NULL,
    owner TEXT NOT NULL CHECK (owner IN ('io', 'moglie')),
    role TEXT NOT NULL DEFAULT 'watch',
    alert_out_btc REAL,
    alert_min_btc REAL,
    hot_threshold_btc REAL,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    owner TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS evm_wallets_addr_owner ON evm_wallets(address, owner);
CREATE UNIQUE INDEX IF NOT EXISTS btc_wallets_desc_owner ON btc_wallets(descriptor, owner);
CREATE INDEX IF NOT EXISTS history_ts ON history(ts);
"""


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        row = conn.execute("SELECT value FROM settings WHERE key='mcp_token'").fetchone()
        if not row:
            token = secrets.token_urlsafe(24)
            conn.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?)",
                ("mcp_token", token),
            )
        defaults = {
            "eth_rpc_url": "",
            "electrs_host": "",
            "electrs_port": "50001",
            "currency": "USD",
            "fx_eur": "0.92",
            "gas_estimate_usd": "",
        }
        for key, value in defaults.items():
            exists = conn.execute("SELECT 1 FROM settings WHERE key=?", (key,)).fetchone()
            if not exists:
                conn.execute("INSERT INTO settings(key, value) VALUES(?, ?)", (key, value))


def get_setting(key: str, default: str = "") -> str:
    with connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


def all_settings() -> dict[str, str]:
    with connect() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    return {r["key"]: r["value"] for r in rows}


def rotate_mcp_token() -> str:
    token = secrets.token_urlsafe(24)
    set_setting("mcp_token", token)
    return token


def list_evm(owner: str | None = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM evm_wallets"
    args: tuple[Any, ...] = ()
    if owner and owner != "tutti":
        sql += " WHERE owner=?"
        args = (owner,)
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql + " ORDER BY id", args).fetchall()]


def list_btc(owner: str | None = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM btc_wallets"
    args: tuple[Any, ...] = ()
    if owner and owner != "tutti":
        sql += " WHERE owner=?"
        args = (owner,)
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql + " ORDER BY id", args).fetchall()]


def add_evm(label: str, address: str, owner: str) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO evm_wallets(label, address, owner, created_at) VALUES(?,?,?,?)",
            (label, address, owner, int(time.time())),
        )
        return int(cur.lastrowid)


def add_btc(
    label: str,
    kind: str,
    descriptor: str,
    owner: str,
    role: str = "watch",
    alert_out_btc: float | None = None,
    alert_min_btc: float | None = None,
    hot_threshold_btc: float | None = None,
) -> int:
    with connect() as conn:
        cur = conn.execute(
            """INSERT INTO btc_wallets(
                label, kind, descriptor, owner, role, alert_out_btc, alert_min_btc, hot_threshold_btc, created_at
            ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                label,
                kind,
                descriptor,
                owner,
                role,
                alert_out_btc,
                alert_min_btc,
                hot_threshold_btc,
                int(time.time()),
            ),
        )
        return int(cur.lastrowid)


def delete_evm(wallet_id: int) -> bool:
    with connect() as conn:
        cur = conn.execute("DELETE FROM evm_wallets WHERE id=?", (wallet_id,))
        return cur.rowcount > 0


def delete_btc(wallet_id: int) -> bool:
    with connect() as conn:
        cur = conn.execute("DELETE FROM btc_wallets WHERE id=?", (wallet_id,))
        return cur.rowcount > 0


def save_history(owner: str, payload: dict[str, Any]) -> None:
    now = int(time.time())
    with connect() as conn:
        last = conn.execute(
            "SELECT ts FROM history WHERE owner=? ORDER BY ts DESC LIMIT 1",
            (owner,),
        ).fetchone()
        if last and now - int(last["ts"]) < 30 * 60:
            return
        conn.execute(
            "INSERT INTO history(ts, owner, payload) VALUES(?,?,?)",
            (now, owner, json.dumps(payload)),
        )


def get_history(owner: str = "tutti", days: int = 90) -> list[dict[str, Any]]:
    cutoff = int(time.time()) - days * 86400
    sql = "SELECT ts, owner, payload FROM history WHERE ts>=?"
    args: list[Any] = [cutoff]
    if owner and owner != "tutti":
        sql += " AND owner=?"
        args.append(owner)
    sql += " ORDER BY ts"
    with connect() as conn:
        rows = conn.execute(sql, args).fetchall()
    out = []
    for r in rows:
        item = json.loads(r["payload"])
        item["ts"] = r["ts"]
        item["owner"] = r["owner"]
        out.append(item)
    return out
