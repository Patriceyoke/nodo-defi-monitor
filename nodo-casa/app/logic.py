"""Scadenze di Casa Viale. Date in Asia/Bangkok, database vuoto finché non arriva un import."""

from __future__ import annotations

import calendar
import os
import secrets
import sqlite3
import threading
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    TZ = ZoneInfo("Asia/Bangkok")
except ZoneInfoNotFoundError:
    TZ = timezone(timedelta(hours=7))

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
DB_PATH = DATA_DIR / "casa.db"
TOKEN_PATH = DATA_DIR / "agent.token"

SECTIONS = ("casa", "garage", "documenti", "immigrazione")
SECTION_NAMES = {
    "casa": "Casa",
    "garage": "Garage",
    "documenti": "Documenti",
    "immigrazione": "Immigrazione",
}
NAV = (
    ("casa", "Casa"),
    ("garage", "Garage"),
    ("documenti", "Documenti"),
    ("immigrazione", "Visto"),
)
MONTHS = ("gen", "feb", "mar", "apr", "mag", "giu", "lug", "ago", "set", "ott", "nov", "dic")
GARAGE_TIPI = {"tagliando": "Tagliando", "bollo": "Bollo", "garanzia": "Garanzia"}
VISTO_TIPI = {"visto": "Visto", "rapporto_90": "Rapporto 90 giorni"}

_LOCK = threading.Lock()


class Invalid(Exception):
    def __init__(self, field: str = "body", external_id: str | None = None):
        super().__init__(field)
        self.field = field
        self.external_id = external_id


IMPORT_EXAMPLE = {
    "items": [
        {
            "external_id": "pwa-acqua",
            "section": "casa",
            "title": "Acqua PWA",
            "person": "",
            "due_day": 10,
            "due_date": "",
            "interval": "monthly",
            "amount_thb": None,
            "notes": "",
            "status": "pending",
        }
    ]
}
INTERVALS = ("monthly", "yearly", "once", "days_90")
STATUSES = ("pending", "paid", "done")


def today_bkk() -> date:
    return datetime.now(TZ).date()


def now_iso() -> str:
    return datetime.now(TZ).replace(microsecond=0).isoformat()


def short_date(iso: str) -> str:
    year, month, day = (int(part) for part in iso.split("-"))
    return f"{day} {MONTHS[month - 1]}"


def distance_words(delta: int) -> str:
    if delta == 0:
        return "oggi"
    if delta > 0:
        unit = "giorno" if delta == 1 else "giorni"
        return f"tra {delta} {unit}"
    gone = -delta
    unit = "giorno" if gone == 1 else "giorni"
    return f"scaduto da {gone} {unit}"


def color_for(delta: int | None, done: bool) -> str | None:
    if done:
        return "green"
    if delta is None:
        return None
    if delta > 30:
        return "green"
    if delta >= 8:
        return "amber"
    return "red"


def format_baht(value) -> str:
    if value is None or value == "" or isinstance(value, bool):
        return ""
    try:
        dec = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return ""
    if not dec.is_finite():
        return ""
    sign = "-" if dec < 0 else ""
    text = format(abs(dec), "f")
    whole, _, frac = text.partition(".")
    frac = frac.rstrip("0")
    groups = []
    while whole:
        groups.append(whole[-3:])
        whole = whole[:-3]
    grouped = ".".join(reversed(groups)) or "0"
    if frac:
        return f"{sign}฿{grouped},{frac}"
    return f"{sign}฿{grouped}"


def amount_label(payload: dict) -> str:
    if not isinstance(payload, dict) or payload.get("amount_thb") is None:
        return ""
    return format_baht(payload.get("amount_thb"))


def _hist(entry, key: str):
    if isinstance(entry, dict):
        return entry.get(key)
    try:
        return entry[key]
    except (KeyError, IndexError):
        return None


def paid_this_month(history: list, today: date) -> bool:
    prefix = f"{today.year:04d}-{today.month:02d}"
    for entry in history:
        if _hist(entry, "kind") not in ("pagato", "fatto"):
            continue
        for key in ("paid_at", "created_at"):
            stamp = _hist(entry, key) or ""
            if isinstance(stamp, str) and stamp.startswith(prefix):
                return True
        if _hist(entry, "period") == prefix:
            return True
    return False


def month_due(year: int, month: int, giorno: int) -> date:
    last = calendar.monthrange(year, month)[1]
    return date(year, month, min(giorno, last))


def ensure_storage() -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SystemExit("Cartella dati non scrivibile") from exc
    if not os.access(DATA_DIR, os.W_OK):
        raise SystemExit("Cartella dati non scrivibile")
    _ensure_token()
    with _db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS items (
              id TEXT PRIMARY KEY,
              external_id TEXT UNIQUE,
              section TEXT NOT NULL,
              payload TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS history (
              id INTEGER PRIMARY KEY,
              item_id TEXT NOT NULL,
              kind TEXT NOT NULL,
              period TEXT,
              paid_at TEXT,
              note TEXT,
              amount_thb TEXT,
              created_at TEXT NOT NULL
            );
            """
        )
        _drop_probe_schema(conn)


def _due_text(payload: dict) -> str:
    for key in ("due_date", "scadenza", "data", "prossima"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _drop_probe_schema(conn: sqlite3.Connection) -> None:
    """Remove one known probe row. Any mismatch leaves the row untouched."""
    import json

    row = conn.execute(
        "SELECT id, payload FROM items WHERE external_id = ?",
        ("probe-schema",),
    ).fetchone()
    if row is None:
        return
    try:
        payload = json.loads(row["payload"])
    except (TypeError, json.JSONDecodeError):
        return
    if not isinstance(payload, dict):
        return
    title = payload.get("title")
    if not isinstance(title, str):
        title = payload.get("titolo")
    if not isinstance(title, str) or title.strip() != "\u2014":
        return
    if _due_text(payload) != "2099-01-01":
        return
    conn.execute("DELETE FROM history WHERE item_id = ?", (row["id"],))
    conn.execute("DELETE FROM items WHERE id = ?", (row["id"],))


def delete_by_external_id(external_id: str) -> bool:
    with _db_lock() as conn:
        row = conn.execute("SELECT id FROM items WHERE external_id = ?", (external_id,)).fetchone()
        if row is None:
            return False
        conn.execute("DELETE FROM history WHERE item_id = ?", (row["id"],))
        conn.execute("DELETE FROM items WHERE id = ?", (row["id"],))
    return True


def _ensure_token() -> None:
    if TOKEN_PATH.exists():
        os.chmod(TOKEN_PATH, 0o600)
        return
    token = secrets.token_urlsafe(48).encode("ascii")
    try:
        fd = os.open(TOKEN_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        os.chmod(TOKEN_PATH, 0o600)
        return
    try:
        os.write(fd, token)
    finally:
        os.close(fd)
    os.chmod(TOKEN_PATH, 0o600)


def token_matches(presented: str) -> bool:
    import hmac

    try:
        expected = TOKEN_PATH.read_text(encoding="ascii").strip()
    except OSError:
        return False
    if not expected or not presented:
        return False
    return hmac.compare_digest(presented, expected)


def token_present() -> bool:
    try:
        return TOKEN_PATH.is_file() and TOKEN_PATH.stat().st_size > 0
    except OSError:
        return False


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        os.chmod(DB_PATH, 0o600)
    except OSError:
        pass
    return conn


class _db_lock:
    def __enter__(self) -> sqlite3.Connection:
        _LOCK.acquire()
        self.conn = _db()
        return self.conn

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if exc_type is None:
                self.conn.commit()
            else:
                self.conn.rollback()
        finally:
            self.conn.close()
            _LOCK.release()


def _text(data: dict, key: str, *, required: bool, limit: int) -> str:
    value = data.get(key)
    if value is None or value == "":
        if required:
            raise Invalid(key)
        return ""
    if not isinstance(value, str):
        raise Invalid(key)
    text = value.strip()
    if required and not text:
        raise Invalid(key)
    if len(text) > limit:
        raise Invalid(key)
    return text


def _date(data: dict, key: str, *, required: bool) -> str | None:
    value = data.get(key)
    if value is None or value == "":
        if required:
            raise Invalid(key)
        return None
    if not isinstance(value, str):
        raise Invalid(key)
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise Invalid(key) from exc


def _amount(value) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise Invalid("importo_thb")
    try:
        dec = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise Invalid("importo_thb") from exc
    if not dec.is_finite() or dec < 0:
        raise Invalid("importo_thb")
    text = format(dec, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _whole(value, key: str, low: int, high: int, *, required: bool) -> int | None:
    if value is None or value == "":
        if required:
            raise Invalid(key)
        return None
    if isinstance(value, bool):
        raise Invalid(key)
    if isinstance(value, float):
        if not value.is_integer():
            raise Invalid(key)
        value = int(value)
    if isinstance(value, str):
        if not value.strip().lstrip("-").isdigit():
            raise Invalid(key)
        value = int(value.strip())
    if not isinstance(value, int) or value < low or value > high:
        raise Invalid(key)
    return value


def _choice(data: dict, key: str, allowed: dict[str, str]) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise Invalid(key)
    norm = value.strip().lower().replace(" ", "_")
    aliases = {
        "tagliando": "tagliando",
        "bollo": "bollo",
        "garanzia": "garanzia",
        "visto": "visto",
        "rapporto_90": "rapporto_90",
        "rapporto_90_giorni": "rapporto_90",
        "rapporto": "rapporto_90",
    }
    code = aliases.get(norm, norm)
    if code not in allowed:
        raise Invalid(key)
    return code


def payload_from(section: str, data: dict) -> dict:
    if section == "casa":
        return {
            "titolo": _text(data, "titolo", required=True, limit=200),
            "giorno": _whole(data.get("giorno"), "giorno", 1, 31, required=True),
            "importo_thb": _amount(data.get("importo_thb")),
            "note": _text(data, "note", required=False, limit=2000),
        }
    if section == "garage":
        payload = {
            "veicolo": _text(data, "veicolo", required=True, limit=200),
            "tipo": _choice(data, "tipo", GARAGE_TIPI),
            "data": _date(data, "data", required=False),
            "km": _whole(data.get("km"), "km", 0, 10_000_000, required=False),
            "note": _text(data, "note", required=False, limit=2000),
        }
        if payload["data"] is None and payload["km"] is None:
            raise Invalid("data")
        return payload
    if section == "documenti":
        return {
            "persona": _text(data, "persona", required=True, limit=200),
            "tipo": _text(data, "tipo", required=True, limit=200),
            "scadenza": _date(data, "scadenza", required=True),
            "numero": _text(data, "numero", required=False, limit=200),
        }
    if section == "immigrazione":
        payload = {
            "tipo": _choice(data, "tipo", VISTO_TIPI),
            "ultimo_rapporto": _date(data, "ultimo_rapporto", required=False),
            "prossima": _date(data, "prossima", required=False),
            "scadenza_visto": _date(data, "scadenza_visto", required=False),
            "note": _text(data, "note", required=False, limit=2000),
        }
        if payload["tipo"] == "visto" and not payload["scadenza_visto"]:
            raise Invalid("scadenza_visto")
        if payload["tipo"] == "rapporto_90" and not (
            payload["ultimo_rapporto"] or payload["prossima"]
        ):
            raise Invalid("prossima")
        return payload
    raise Invalid("section")


def _section_of(data: dict) -> str:
    section = data.get("section")
    if section not in SECTIONS:
        raise Invalid("section")
    return section


def _external_id(data: dict, *, required: bool) -> str | None:
    value = data.get("external_id")
    if value is None or value == "":
        if required:
            raise Invalid("external_id")
        return None
    if not isinstance(value, str):
        raise Invalid("external_id")
    text = value.strip()
    if not text or len(text) > 200:
        raise Invalid("external_id")
    return text


def compute_due(section: str, payload: dict, today: date) -> date | None:
    interval = payload.get("interval")
    if interval == "monthly" and payload.get("due_day"):
        return month_due(today.year, today.month, int(payload["due_day"]))
    if interval in {"yearly", "once", "days_90"} and payload.get("due_date"):
        return date.fromisoformat(payload["due_date"])
    if section == "casa" and payload.get("giorno"):
        return month_due(today.year, today.month, int(payload["giorno"]))
    if section == "garage":
        if payload.get("data"):
            return date.fromisoformat(payload["data"])
        return None
    if section == "documenti":
        return date.fromisoformat(payload["scadenza"])
    if payload["tipo"] == "visto":
        return date.fromisoformat(payload["scadenza_visto"])
    if payload.get("prossima"):
        return date.fromisoformat(payload["prossima"])
    if payload.get("ultimo_rapporto"):
        return date.fromisoformat(payload["ultimo_rapporto"]) + timedelta(days=90)
    return None


def period_for(section: str, payload: dict, today: date) -> str:
    if payload.get("interval") == "monthly" or (
        section == "casa" and payload.get("giorno") and not payload.get("interval")
    ):
        return f"{today.year:04d}-{today.month:02d}"
    due = compute_due(section, payload, today)
    if due is not None:
        return due.isoformat()
    if section == "garage" and payload.get("km") is not None:
        return f"km:{payload['km']}"
    return "aperto"


def _label(section: str, payload: dict) -> str:
    if payload.get("title"):
        return payload["title"]
    if section == "casa":
        return payload["titolo"]
    if section == "garage":
        return payload["veicolo"]
    if section == "documenti":
        return payload["tipo"]
    return VISTO_TIPI[payload["tipo"]]


def _load(conn: sqlite3.Connection) -> list[dict]:
    import json

    rows = conn.execute(
        "SELECT id, external_id, section, payload, created_at, updated_at FROM items"
    ).fetchall()
    history_rows = conn.execute(
        "SELECT item_id, kind, period, paid_at, note, amount_thb, created_at FROM history ORDER BY id"
    ).fetchall()
    grouped: dict[str, list] = {}
    for row in history_rows:
        grouped.setdefault(row["item_id"], []).append(row)
    items = []
    for row in rows:
        items.append(
            {
                "id": row["id"],
                "external_id": row["external_id"],
                "section": row["section"],
                "payload": json.loads(row["payload"]),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "history": list(grouped.get(row["id"], [])),
            }
        )
    return items


def _present(item: dict, today: date) -> dict:
    section = item["section"]
    payload = item["payload"]
    period = period_for(section, payload, today)
    done = any(entry["period"] == period and entry["kind"] in ("pagato", "fatto") for entry in item["history"])
    due = compute_due(section, payload, today)
    delta = (due - today).days if due is not None else None
    color = color_for(delta, done)
    kind = "pagato" if section == "casa" else "fatto"
    if done:
        stato = kind
        stato_label = "Pagato" if section == "casa" else "Fatto"
    else:
        stato = "da_pagare" if section == "casa" else "da_fare"
        stato_label = "Da pagare" if section == "casa" else "Da fare"
    history = []
    for entry in item["history"]:
        history.append(
            {
                "kind": entry["kind"],
                "kind_label": "Pagato" if entry["kind"] == "pagato" else "Fatto",
                "period": entry["period"],
                "paid_at": entry["paid_at"],
                "paid_label": short_date(entry["paid_at"]) if entry["paid_at"] else "",
                "note": entry["note"] or "",
                "amount_thb": entry["amount_thb"],
            }
        )
    return {
        "id": item["id"],
        "external_id": item["external_id"],
        "section": section,
        "section_name": SECTION_NAMES[section],
        "payload": payload,
        "label": _label(section, payload),
        "period": period,
        "done": done,
        "stato": stato,
        "stato_label": stato_label,
        "due": due.isoformat() if due else None,
        "due_label": short_date(due.isoformat()) if due else "",
        "delta": delta,
        "distance": distance_words(delta) if delta is not None and not done else "",
        "color": color,
        "amount_label": amount_label(payload),
        "paid_this_month": paid_this_month(item["history"], today),
        "history": history,
        "created_at": item["created_at"],
        "updated_at": item["updated_at"],
    }


def _sort_key(view: dict) -> tuple:
    if view["done"]:
        return (2, view["delta"] if view["delta"] is not None else 10_000, view["label"])
    if view["delta"] is None:
        return (1, 10_000, view["label"])
    return (0, view["delta"], view["label"])


def home() -> dict:
    today = today_bkk()
    with _db_lock() as conn:
        views = [_present(item, today) for item in _load(conn)]
    sections = []
    for sid in SECTIONS:
        group = [view for view in views if view["section"] == sid]
        red = sum(1 for view in group if view["color"] == "red")
        open_dated = sorted(
            (view for view in group if not view["done"] and view["delta"] is not None),
            key=lambda view: (view["delta"], view["label"]),
        )
        nxt = open_dated[0] if open_dated else None
        sections.append(
            {
                "id": sid,
                "name": SECTION_NAMES[sid],
                "empty": not group,
                "red": red,
                "all_done": bool(group) and all(view["done"] for view in group),
                "done_word": "Pagato" if sid == "casa" else "Fatto",
                "next": None
                if nxt is None
                else {
                    "id": nxt["id"],
                    "label": nxt["label"],
                    "due_label": nxt["due_label"],
                    "distance": nxt["distance"],
                    "color": nxt["color"],
                },
            }
        )
    upcoming = [
        view
        for view in views
        if not view["done"] and view["delta"] is not None and view["delta"] <= 30
    ]
    upcoming.sort(key=lambda view: (view["delta"], view["label"]))
    return {
        "empty": not views,
        "sections": sections,
        "upcoming": [
            {
                "id": view["id"],
                "section": view["section"],
                "section_name": view["section_name"],
                "label": view["label"],
                "due_label": view["due_label"],
                "distance": view["distance"],
                "amount_label": view["amount_label"],
                "color": view["color"],
            }
            for view in upcoming[:5]
        ],
    }


def section_items(section: str) -> list[dict]:
    if section not in SECTIONS:
        raise Invalid("section")
    today = today_bkk()
    with _db_lock() as conn:
        views = [_present(item, today) for item in _load(conn) if item["section"] == section]
    views.sort(key=_sort_key)
    return views


def get_item(item_id: str) -> dict | None:
    today = today_bkk()
    with _db_lock() as conn:
        for item in _load(conn):
            if item["id"] == item_id:
                return _present(item, today)
    return None


def create_item(data: dict) -> dict:
    import json

    section = _section_of(data)
    payload = payload_from(section, data)
    external_id = _external_id(data, required=False)
    item_id = str(uuid4())
    stamp = now_iso()
    with _db_lock() as conn:
        if external_id:
            found = conn.execute("SELECT 1 FROM items WHERE external_id = ?", (external_id,)).fetchone()
            if found:
                raise Invalid("external_id")
        conn.execute(
            "INSERT INTO items (id, external_id, section, payload, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (item_id, external_id, section, json.dumps(payload, ensure_ascii=False), stamp, stamp),
        )
    item = get_item(item_id)
    assert item is not None
    return item


def _attach_external_id(raw: dict, exc: Invalid) -> None:
    if exc.external_id or exc.field == "external_id":
        return
    value = raw.get("external_id")
    if isinstance(value, str) and value.strip():
        exc.external_id = value.strip()


def _import_amount(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise Invalid("amount_thb")
    try:
        dec = Decimal(str(value))
    except InvalidOperation as exc:
        raise Invalid("amount_thb") from exc
    if not dec.is_finite() or dec < 0:
        raise Invalid("amount_thb")
    text = format(dec, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def normalize_import_item(raw: dict) -> tuple[str, str, dict, str]:
    if not isinstance(raw, dict):
        raise Invalid("items")
    for key in (
        "external_id",
        "section",
        "title",
        "person",
        "due_day",
        "due_date",
        "interval",
        "amount_thb",
        "notes",
        "status",
    ):
        if key not in raw:
            raise Invalid(key)
    external_id = raw["external_id"]
    if not isinstance(external_id, str) or not external_id.strip() or len(external_id.strip()) > 200:
        raise Invalid("external_id")
    external_id = external_id.strip()
    section = raw["section"]
    if section not in SECTIONS:
        raise Invalid("section")
    title = raw["title"]
    if not isinstance(title, str) or not title.strip() or len(title.strip()) > 200:
        raise Invalid("title")
    title = title.strip()
    person = raw["person"]
    if not isinstance(person, str) or len(person.strip()) > 200:
        raise Invalid("person")
    person = person.strip()
    due_day = raw["due_day"]
    if due_day is not None and (
        isinstance(due_day, bool) or not isinstance(due_day, int) or due_day < 1 or due_day > 31
    ):
        raise Invalid("due_day")
    due_date = raw["due_date"]
    if not isinstance(due_date, str):
        raise Invalid("due_date")
    if due_date:
        try:
            due_date = date.fromisoformat(due_date).isoformat()
        except ValueError as exc:
            raise Invalid("due_date") from exc
    interval = raw["interval"]
    if interval not in INTERVALS:
        raise Invalid("interval")
    amount = _import_amount(raw["amount_thb"])
    amount_number = None if amount is None else (int(amount) if "." not in amount else float(amount))
    notes = raw["notes"]
    if not isinstance(notes, str) or len(notes) > 2000:
        raise Invalid("notes")
    status = raw["status"]
    if status not in STATUSES:
        raise Invalid("status")
    if interval == "monthly":
        if due_day is None:
            raise Invalid("due_day")
        if due_date:
            raise Invalid("due_date")
    else:
        if not due_date:
            raise Invalid("due_date")
        if due_day is not None:
            raise Invalid("due_day")
    payload: dict = {
        "title": title,
        "person": person,
        "due_day": due_day,
        "due_date": due_date,
        "interval": interval,
        "amount_thb": amount_number,
        "notes": notes,
        "status": status,
        "note": notes,
    }
    if section == "casa":
        payload["titolo"] = title
        if due_day is not None:
            payload["giorno"] = due_day
        payload["importo_thb"] = amount
    elif section == "garage":
        payload["veicolo"] = title
        if due_date:
            payload["data"] = due_date
    elif section == "documenti":
        payload["persona"] = person
        payload["tipo"] = title
        payload["scadenza"] = due_date
        payload["numero"] = ""
    else:
        payload["tipo"] = "rapporto_90" if interval == "days_90" else "visto"
        payload["note"] = notes
        if interval == "days_90":
            payload["prossima"] = due_date
            payload["ultimo_rapporto"] = None
            payload["scadenza_visto"] = None
        else:
            payload["scadenza_visto"] = due_date
            payload["prossima"] = None
            payload["ultimo_rapporto"] = None
    return section, external_id, payload, status


def _apply_import_status(conn: sqlite3.Connection, item_id: str, section: str, payload: dict, status: str) -> None:
    today = today_bkk()
    period = period_for(section, payload, today)
    if status == "pending":
        conn.execute("DELETE FROM history WHERE item_id = ? AND period = ?", (item_id, period))
        return
    kind = "pagato" if status == "paid" else "fatto"
    found = conn.execute(
        "SELECT 1 FROM history WHERE item_id = ? AND period = ? AND kind IN ('pagato', 'fatto')",
        (item_id, period),
    ).fetchone()
    if found:
        return
    conn.execute(
        """
        INSERT INTO history (item_id, kind, period, paid_at, note, amount_thb, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (item_id, kind, period, today.isoformat(), "", payload.get("importo_thb"), now_iso()),
    )


def import_items(body: dict) -> dict:
    import json

    raw_items = body.get("items") if isinstance(body, dict) else None
    if not isinstance(raw_items, list):
        raise Invalid("items")
    prepared = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            raise Invalid("items")
        try:
            prepared.append(normalize_import_item(raw))
        except Invalid as exc:
            _attach_external_id(raw, exc)
            raise
    created = []
    updated = []
    stamp = now_iso()
    with _db_lock() as conn:
        for section, external_id, payload, status in prepared:
            encoded = json.dumps(payload, ensure_ascii=False)
            row = conn.execute("SELECT id FROM items WHERE external_id = ?", (external_id,)).fetchone()
            if row:
                item_id = row["id"]
                conn.execute(
                    "UPDATE items SET section = ?, payload = ?, updated_at = ? WHERE id = ?",
                    (section, encoded, stamp, item_id),
                )
                updated.append(item_id)
            else:
                item_id = str(uuid4())
                conn.execute(
                    "INSERT INTO items (id, external_id, section, payload, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (item_id, external_id, section, encoded, stamp, stamp),
                )
                created.append(item_id)
            _apply_import_status(conn, item_id, section, payload, status)
    return {"created": created, "updated": updated}


def patch_item(item_id: str, data: dict) -> dict | None:
    import json

    if not isinstance(data, dict):
        raise Invalid("body")
    with _db_lock() as conn:
        row = conn.execute("SELECT section, payload, external_id FROM items WHERE id = ?", (item_id,)).fetchone()
        if row is None:
            return None
        current = json.loads(row["payload"])
        section = data.get("section", row["section"])
        if section not in SECTIONS:
            raise Invalid("section")
        if current.get("interval"):
            payload = dict(current)
            if "note" in data or "notes" in data:
                note = data["notes"] if "notes" in data else data["note"]
                if not isinstance(note, str):
                    raise Invalid("notes")
                payload["notes"] = note
                payload["note"] = note
            if "status" in data:
                if data["status"] not in STATUSES:
                    raise Invalid("status")
                payload["status"] = data["status"]
        else:
            merged = dict(current)
            for key, value in data.items():
                if key in ("id", "section", "external_id"):
                    continue
                merged[key] = value
            payload = payload_from(section, merged)
        external_id = row["external_id"]
        if "external_id" in data:
            external_id = _external_id(data, required=False)
            if external_id:
                clash = conn.execute(
                    "SELECT id FROM items WHERE external_id = ? AND id <> ?",
                    (external_id, item_id),
                ).fetchone()
                if clash:
                    raise Invalid("external_id")
        conn.execute(
            "UPDATE items SET section = ?, external_id = ?, payload = ?, updated_at = ? WHERE id = ?",
            (section, external_id, json.dumps(payload, ensure_ascii=False), now_iso(), item_id),
        )
    return get_item(item_id)


def mark_done(item_id: str, body: dict | None) -> dict | None:
    body = body or {}
    if not isinstance(body, dict):
        raise Invalid("body")
    today = today_bkk()
    paid_at = body.get("paid_at")
    if paid_at in (None, ""):
        paid_at_iso = today.isoformat()
    else:
        if not isinstance(paid_at, str):
            raise Invalid("paid_at")
        try:
            paid_at_iso = date.fromisoformat(paid_at).isoformat()
        except ValueError as exc:
            raise Invalid("paid_at") from exc
    note = _text(body, "note", required=False, limit=2000) if body.get("note") not in (None, "") else ""
    amount = _amount(body.get("amount_thb")) if "amount_thb" in body else None
    with _db_lock() as conn:
        found = None
        for item in _load(conn):
            if item["id"] == item_id:
                found = item
                break
        if found is None:
            return None
        period = period_for(found["section"], found["payload"], today)
        kind = "pagato" if found["section"] == "casa" else "fatto"
        already = any(entry["period"] == period and entry["kind"] == kind for entry in found["history"])
        if not already:
            conn.execute(
                """
                INSERT INTO history (item_id, kind, period, paid_at, note, amount_thb, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (item_id, kind, period, paid_at_iso, note, amount, now_iso()),
            )
    return get_item(item_id)


def export_all() -> dict:
    today = today_bkk()
    with _db_lock() as conn:
        views = [_present(item, today) for item in _load(conn)]
    views.sort(key=lambda view: (SECTIONS.index(view["section"]), view["label"]))
    history = []
    items = []
    for view in views:
        items.append(_public_item(view))
        for entry in view["history"]:
            history.append({"item_id": view["id"], **entry})
    return {"items": items, "history": history}


def _public_item(view: dict) -> dict:
    return {
        "id": view["id"],
        "external_id": view["external_id"],
        "section": view["section"],
        "stato": view["stato"],
        "stato_label": view["stato_label"],
        "color": view["color"],
        "due": view["due"],
        "due_label": view["due_label"],
        "distance": view["distance"],
        "done": view["done"],
        "fields": view["payload"],
        "history": view["history"],
    }


def public_item(view: dict) -> dict:
    return _public_item(view)
