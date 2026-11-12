"""Local SQLite storage. Wallets, settings and history stay on disk only."""
from __future__ import annotations

import json
import secrets
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Iterator

from . import config

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
CREATE TABLE IF NOT EXISTS nft_hides (
    kind TEXT NOT NULL CHECK (kind IN ('token', 'collection')),
    chain TEXT NOT NULL,
    contract TEXT NOT NULL,
    token_id TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    PRIMARY KEY (kind, chain, contract, token_id)
);
"""


def _open_connection() -> sqlite3.Connection:
    """Open SQLite. A read-only data dir switches to a temp dir instead of crashing."""
    last_error: Exception | None = None
    for attempt in (1, 2):
        if attempt == 2:
            config.abandon_unwritable_data_dir(last_error)
        else:
            config.ensure_data_dir()
        path = config.DB_PATH
        conn: sqlite3.Connection | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(path), timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            return conn
        except (OSError, sqlite3.Error) as exc:
            if conn is not None:
                conn.close()
            last_error = exc
            if attempt == 2:
                raise
    raise RuntimeError("database unavailable")


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    conn = _open_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


def init_db() -> None:
    """Create missing tables only.

    This must never DROP, recreate, or empty evm_wallets or btc_wallets.
    App updates call init_db on startup and the saved wallets have to stay.
    """
    if "DROP TABLE" in SCHEMA.upper():
        raise RuntimeError("Lo schema non può cancellare tabelle: i portafogli devono restare.")
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
            "fx_thb": "",
            "gas_estimate_usd": "",
            "nft_api_provider": "",
            "nft_api_key": "",
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


# Settings that are local secrets or regenerated. They stay out of a backup file.
_BACKUP_SKIP_SETTINGS = {"mcp_token"}


def export_backup() -> dict[str, Any]:
    """Wallets and settings as JSON. The MCP token is omitted."""
    settings = {k: v for k, v in all_settings().items() if k not in _BACKUP_SKIP_SETTINGS}
    return {
        "kind": "nodo-defi-monitor-backup",
        "version": 1,
        "exported_at": int(time.time()),
        "evm": [
            {"label": w["label"], "address": w["address"], "owner": w["owner"]}
            for w in list_evm()
        ],
        "btc": [
            {
                "label": w["label"],
                "descriptor": w["descriptor"],
                "owner": w["owner"],
                "role": w.get("role") or "watch",
                "alert_out_btc": w.get("alert_out_btc"),
                "alert_min_btc": w.get("alert_min_btc"),
                "hot_threshold_btc": w.get("hot_threshold_btc"),
            }
            for w in list_btc()
        ],
        "settings": settings,
        "nft_hides": list_nft_hides(),
    }


def import_backup(payload: dict[str, Any]) -> dict[str, int]:
    """Merge a backup. Existing wallets are kept; nothing is deleted."""
    if not isinstance(payload, dict):
        raise ValueError("Il file non è un oggetto JSON.")
    added_evm = 0
    added_btc = 0
    existing_evm = {(w["address"].lower(), w["owner"]) for w in list_evm()}
    existing_btc = {(w["descriptor"].strip(), w["owner"]) for w in list_btc()}
    for row in payload.get("evm") or []:
        if not isinstance(row, dict):
            continue
        key = (str(row.get("address") or "").lower(), str(row.get("owner") or ""))
        if key in existing_evm or not key[0]:
            continue
        add_evm(str(row.get("label") or "Importato"), key[0], key[1])
        existing_evm.add(key)
        added_evm += 1
    for row in payload.get("btc") or []:
        if not isinstance(row, dict):
            continue
        descriptor = str(row.get("descriptor") or "").strip()
        owner = str(row.get("owner") or "")
        if not descriptor or (descriptor, owner) in existing_btc:
            continue
        add_btc(
            str(row.get("label") or "Importato"),
            str(row.get("kind") or "xpub"),
            descriptor,
            owner,
            role=str(row.get("role") or "watch"),
            alert_out_btc=row.get("alert_out_btc"),
            alert_min_btc=row.get("alert_min_btc"),
            hot_threshold_btc=row.get("hot_threshold_btc"),
        )
        existing_btc.add((descriptor, owner))
        added_btc += 1
    updated = 0
    settings = payload.get("settings") or {}
    if isinstance(settings, dict):
        for key, value in settings.items():
            if key in _BACKUP_SKIP_SETTINGS or not isinstance(key, str):
                continue
            set_setting(key, "" if value is None else str(value))
            updated += 1
    hides_added = 0
    for row in payload.get("nft_hides") or []:
        if not isinstance(row, dict):
            continue
        if hide_nft(str(row.get("chain") or ""), str(row.get("contract") or ""), str(row.get("token_id") or ""), str(row.get("kind") or "token")):
            hides_added += 1
    return {"evm_added": added_evm, "btc_added": added_btc, "settings_updated": updated, "nft_hides_added": hides_added}


def list_nft_hides() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT kind, chain, contract, token_id, created_at FROM nft_hides ORDER BY created_at, contract, token_id"
        ).fetchall()
    return [dict(r) for r in rows]


def hide_nft(chain: str, contract: str, token_id: str = "", kind: str = "token") -> bool:
    """Remember a local hide. Returns False if that hide was already stored."""
    chain = (chain or "").strip().lower()
    contract = (contract or "").strip().lower()
    token_id = "" if kind == "collection" else str(token_id or "").strip()
    kind = "collection" if kind == "collection" else "token"
    if not chain or not contract:
        raise ValueError("Catena o contratto mancante.")
    with connect() as conn:
        cur = conn.execute(
            """INSERT INTO nft_hides(kind, chain, contract, token_id, created_at)
               VALUES(?,?,?,?,?)
               ON CONFLICT(kind, chain, contract, token_id) DO NOTHING""",
            (kind, chain, contract, token_id, int(time.time())),
        )
        return cur.rowcount > 0


def unhide_nft(chain: str, contract: str, token_id: str = "", kind: str = "token") -> bool:
    chain = (chain or "").strip().lower()
    contract = (contract or "").strip().lower()
    token_id = "" if kind == "collection" else str(token_id or "").strip()
    kind = "collection" if kind == "collection" else "token"
    with connect() as conn:
        cur = conn.execute(
            "DELETE FROM nft_hides WHERE kind=? AND chain=? AND contract=? AND token_id=?",
            (kind, chain, contract, token_id),
        )
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
