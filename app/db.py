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
CREATE TABLE IF NOT EXISTS balance_marks (
    wallet TEXT NOT NULL,
    chain TEXT NOT NULL,
    token TEXT NOT NULL,
    owner TEXT NOT NULL,
    symbol TEXT NOT NULL,
    amount REAL NOT NULL,
    usd REAL NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (wallet, chain, token)
);
CREATE TABLE IF NOT EXISTS app_cache (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS switch_hides (
    owner TEXT NOT NULL,
    from_position TEXT NOT NULL,
    to_target TEXT NOT NULL,
    spread REAL NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (owner, from_position, to_target)
);
CREATE TABLE IF NOT EXISTS approval_hides (
    wallet TEXT NOT NULL,
    chain TEXT NOT NULL,
    token TEXT NOT NULL,
    spender TEXT NOT NULL,
    allowance TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (wallet, chain, token, spender)
);
CREATE TABLE IF NOT EXISTS position_marks (
    position_key TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    protocol TEXT NOT NULL,
    asset TEXT NOT NULL,
    wallet TEXT NOT NULL,
    chain TEXT NOT NULL,
    amount REAL NOT NULL,
    usd REAL NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS fund_flows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    flow_key TEXT NOT NULL UNIQUE,
    direction TEXT NOT NULL CHECK (direction IN ('out', 'in')),
    owner TEXT NOT NULL,
    protocol TEXT NOT NULL,
    asset TEXT NOT NULL,
    wallet TEXT NOT NULL,
    chain TEXT NOT NULL,
    previous_amount REAL NOT NULL,
    amount REAL NOT NULL,
    delta REAL NOT NULL,
    usd_delta REAL NOT NULL,
    detected_at INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'mine', 'suspicious', 'noted'))
);
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
            "btc_gap_limit": "50",
            "outflow_drop_pct": "2",
            "outflow_usd": "500",
            "currency": "USD",
            "fx_thb": "",
            "gas_estimate_usd": "",
            "nft_api_provider": "",
            "nft_api_key": "",
            "positions_open": "1",
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


def update_evm(wallet_id: int, label: str, address: str, owner: str) -> bool:
    """Update one saved address in place.

    A label or owner change keeps the same row, so balance marks and the
    owner history stay linked to that address. A new address string is stored
    on the same row with the chosen label and owner, and the old marks are
    left where they were.
    """
    label = (label or "").strip()[:80]
    if not label:
        raise ValueError("Scrivi un'etichetta.")
    address = address.lower()
    with connect() as conn:
        row = conn.execute(
            "SELECT id, address, owner FROM evm_wallets WHERE id=?",
            (wallet_id,),
        ).fetchone()
        if not row:
            return False
        clash = conn.execute(
            "SELECT id FROM evm_wallets WHERE address=? AND owner=? AND id<>?",
            (address, owner, wallet_id),
        ).fetchone()
        if clash:
            raise ValueError("Questo indirizzo è già salvato per questo titolare.")
        try:
            conn.execute(
                "UPDATE evm_wallets SET label=?, address=?, owner=? WHERE id=?",
                (label, address, owner, wallet_id),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError("Questo indirizzo è già salvato per questo titolare.") from exc
        old_address = str(row["address"]).lower()
        if old_address == address and str(row["owner"]) != owner:
            conn.execute(
                "UPDATE balance_marks SET owner=? WHERE wallet=?",
                (owner, old_address),
            )
        return True


# Settings that are local secrets, regenerated, or leftover Bitcoin options.
# Bitcoin rows stay in the database and are never exported, imported, or shown.
_BACKUP_SKIP_SETTINGS = {"mcp_token", "electrs_host", "electrs_port", "btc_gap_limit", "tor_enabled"}


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
        "btc": [],
        "settings": settings,
        "nft_hides": list_nft_hides(),
        "approval_hides": list_approval_hides(),
        "switch_hides": list_switch_hides(),
        "warning": (
            "Questo file contiene gli indirizzi Ethereum pubblici salvati su questo server "
            "e, se l'hai impostata, la chiave API NFT. Non contiene il token MCP. "
            "Tienilo solo sul tuo computer."
        ),
    }


def import_backup(payload: dict[str, Any]) -> dict[str, int]:
    """Merge a backup. Existing wallets are kept; nothing is deleted."""
    if not isinstance(payload, dict):
        raise ValueError("Il file non è un oggetto JSON.")
    added_evm = 0
    existing_evm = {(w["address"].lower(), w["owner"]) for w in list_evm()}
    for row in payload.get("evm") or []:
        if not isinstance(row, dict):
            continue
        key = (str(row.get("address") or "").lower(), str(row.get("owner") or ""))
        if key in existing_evm or not key[0]:
            continue
        add_evm(str(row.get("label") or "Importato"), key[0], key[1])
        existing_evm.add(key)
        added_evm += 1
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
    switches_added = 0
    for row in payload.get("switch_hides") or []:
        if not isinstance(row, dict):
            continue
        if hide_switch(
            str(row.get("owner") or ""),
            str(row.get("from_position") or ""),
            str(row.get("to_target") or ""),
            float(row.get("spread") or 0),
        ):
            switches_added += 1
    approvals_added = 0
    for row in payload.get("approval_hides") or []:
        if not isinstance(row, dict):
            continue
        if hide_approval(
            str(row.get("wallet") or ""),
            str(row.get("chain") or ""),
            str(row.get("token") or ""),
            str(row.get("spender") or ""),
            str(row.get("allowance") or ""),
        ):
            approvals_added += 1
    return {
        "evm_added": added_evm,
        "btc_added": 0,
        "settings_updated": updated,
        "nft_hides_added": hides_added,
        "approval_hides_added": approvals_added,
        "switch_hides_added": switches_added,
    }


def export_addresses() -> dict[str, Any]:
    """Addresses, owners and labels only. No settings, hides or tokens."""
    return {
        "kind": "nodo-defi-monitor-addresses",
        "version": 1,
        "addresses": [
            {"address": row["address"], "owner": row["owner"], "label": row["label"]}
            for row in list_evm()
        ],
    }


def import_addresses(rows: list[dict[str, Any]]) -> dict[str, int]:
    """Add public addresses. Existing address+owner pairs are left as they are."""
    added = 0
    skipped = 0
    existing = {(row["address"].lower(), row["owner"]) for row in list_evm()}
    for row in rows:
        key = (str(row.get("address") or "").lower(), str(row.get("owner") or ""))
        if not key[0] or not key[1] or key in existing:
            skipped += 1
            continue
        add_evm(str(row.get("label") or "Importato")[:80], key[0], key[1])
        existing.add(key)
        added += 1
    return {"added": added, "skipped": skipped}


def list_balance_marks() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT wallet, chain, token, owner, symbol, amount, usd, updated_at FROM balance_marks"
        ).fetchall()
    return [dict(row) for row in rows]


def upsert_balance_mark(
    wallet: str, chain: str, token: str, owner: str, symbol: str, amount: float, usd: float
) -> None:
    now = int(time.time())
    with connect() as conn:
        conn.execute(
            """INSERT INTO balance_marks(wallet, chain, token, owner, symbol, amount, usd, updated_at)
               VALUES(?,?,?,?,?,?,?,?)
               ON CONFLICT(wallet, chain, token) DO UPDATE SET
                 owner=excluded.owner, symbol=excluded.symbol, amount=excluded.amount,
                 usd=excluded.usd, updated_at=excluded.updated_at""",
            (wallet.lower(), chain, token.lower(), owner, symbol, float(amount), float(usd), now),
        )


def list_position_marks() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT position_key, owner, protocol, asset, wallet, chain, amount, usd, updated_at FROM position_marks"
        ).fetchall()
    return [dict(row) for row in rows]


def upsert_position_mark(
    position_key: str, owner: str, protocol: str, asset: str, wallet: str, chain: str, amount: float, usd: float
) -> None:
    now = int(time.time())
    with connect() as conn:
        conn.execute(
            """INSERT INTO position_marks(position_key, owner, protocol, asset, wallet, chain, amount, usd, updated_at)
               VALUES(?,?,?,?,?,?,?,?,?)
               ON CONFLICT(position_key) DO UPDATE SET
                 owner=excluded.owner, protocol=excluded.protocol, asset=excluded.asset,
                 wallet=excluded.wallet, chain=excluded.chain, amount=excluded.amount,
                 usd=excluded.usd, updated_at=excluded.updated_at""",
            (position_key, owner, protocol, asset, wallet.lower(), chain, float(amount), float(usd), now),
        )


def list_fund_flows() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """SELECT id, flow_key, direction, owner, protocol, asset, wallet, chain,
                      previous_amount, amount, delta, usd_delta, detected_at, status
               FROM fund_flows ORDER BY detected_at DESC, id DESC"""
        ).fetchall()
    return [dict(row) for row in rows]


def insert_fund_flow(row: dict[str, Any]) -> bool:
    """Store one quantity change. Returns False when that exact change is already stored."""
    with connect() as conn:
        cur = conn.execute(
            """INSERT INTO fund_flows(
                 flow_key, direction, owner, protocol, asset, wallet, chain,
                 previous_amount, amount, delta, usd_delta, detected_at, status
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(flow_key) DO NOTHING""",
            (
                row["flow_key"], row["direction"], row["owner"], row["protocol"], row["asset"],
                str(row.get("wallet") or "").lower(), row["chain"],
                float(row["previous_amount"]), float(row["amount"]), float(row["delta"]),
                float(row["usd_delta"]), int(row["detected_at"]), row["status"],
            ),
        )
        return cur.rowcount > 0


def set_fund_flow_status(flow_id: int, status: str) -> bool:
    if status not in {"mine", "suspicious"}:
        raise ValueError("Risposta non valida.")
    with connect() as conn:
        cur = conn.execute(
            "UPDATE fund_flows SET status=? WHERE id=? AND direction='out'",
            (status, int(flow_id)),
        )
        return cur.rowcount > 0


def cache_get(key: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT value, updated_at FROM app_cache WHERE key=?", (key,)).fetchone()
    if not row:
        return None
    try:
        payload = json.loads(row["value"])
    except json.JSONDecodeError:
        return None
    if isinstance(payload, dict):
        payload.setdefault("cached_at", row["updated_at"])
    return payload


def cache_delete_prefix(prefix: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM app_cache WHERE key LIKE ?", (prefix + "%",))


def cache_set(key: str, payload: dict[str, Any]) -> None:
    now = int(time.time())
    blob = json.dumps(payload)
    with connect() as conn:
        conn.execute(
            """INSERT INTO app_cache(key, value, updated_at) VALUES(?,?,?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
            (key, blob, now),
        )


def list_switch_hides() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT owner, from_position, to_target, spread, created_at FROM switch_hides ORDER BY created_at"
        ).fetchall()
    return [dict(row) for row in rows]


def hide_switch(owner: str, from_position: str, to_target: str, spread: float) -> bool:
    owner = (owner or "").strip().lower()
    from_position = (from_position or "").strip()
    to_target = (to_target or "").strip()
    if owner not in {"io", "moglie"} or not from_position or not to_target:
        raise ValueError("Suggerimento non valido.")
    with connect() as conn:
        conn.execute(
            """INSERT INTO switch_hides(owner, from_position, to_target, spread, created_at)
               VALUES(?,?,?,?,?)
               ON CONFLICT(owner, from_position, to_target) DO UPDATE SET
                 spread=excluded.spread, created_at=excluded.created_at""",
            (owner, from_position, to_target, float(spread), int(time.time())),
        )
    return True


def unhide_switch(owner: str, from_position: str, to_target: str) -> bool:
    with connect() as conn:
        cur = conn.execute(
            "DELETE FROM switch_hides WHERE owner=? AND from_position=? AND to_target=?",
            ((owner or "").strip().lower(), (from_position or "").strip(), (to_target or "").strip()),
        )
        return cur.rowcount > 0


def list_approval_hides() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT wallet, chain, token, spender, allowance, created_at FROM approval_hides ORDER BY created_at"
        ).fetchall()
    return [dict(row) for row in rows]


def hide_approval(wallet: str, chain: str, token: str, spender: str, allowance: str) -> bool:
    wallet = (wallet or "").strip().lower()
    chain = (chain or "").strip().lower()
    token = (token or "").strip().lower()
    spender = (spender or "").strip().lower()
    if not wallet or not chain or not token or not spender:
        raise ValueError("Dati dell'approvazione incompleti.")
    with connect() as conn:
        conn.execute(
            """INSERT INTO approval_hides(wallet, chain, token, spender, allowance, created_at)
               VALUES(?,?,?,?,?,?)
               ON CONFLICT(wallet, chain, token, spender) DO UPDATE SET
                 allowance=excluded.allowance, created_at=excluded.created_at""",
            (wallet, chain, token, spender, str(allowance), int(time.time())),
        )
    return True


def unhide_approval(wallet: str, chain: str, token: str, spender: str) -> bool:
    with connect() as conn:
        cur = conn.execute(
            "DELETE FROM approval_hides WHERE wallet=? AND chain=? AND token=? AND spender=?",
            ((wallet or "").lower(), (chain or "").lower(), (token or "").lower(), (spender or "").lower()),
        )
        return cur.rowcount > 0


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
