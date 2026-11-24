"""Scadenze di Casa Viale. Date in Asia/Bangkok, database vuoto finché non arriva un import."""

from __future__ import annotations

import calendar
import os
import re
import secrets
import sqlite3
import threading
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
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
    ("spese", "Spese"),
)
CATEGORIES = ("bollette", "condominio", "abbonamenti", "garage", "documenti", "altro")
CATEGORY_LABELS = {
    "bollette": "Casa / bollette",
    "condominio": "Condominio",
    "abbonamenti": "Abbonamenti",
    "garage": "Garage",
    "documenti": "Documenti / Visto",
    "altro": "Altro",
    "carte": "Carte",
}
CARD_KINDS = ("credito", "debito")
CARD_CURRENCIES = ("THB", "USD", "EUR")
FORBIDDEN_CARD_KEYS = (
    "number", "card_number", "pan", "cvv", "cvc",
    "expiry", "expiration", "pin", "iban", "account", "account_id",
)
CANONICAL_CARDS = (
    ("kast", "Kast Finance", "debito", "USD"),
    ("etherfi", "Ether.fi", "debito", "USD"),
    ("bbl-visa", "Bangkok Bank", "credito", "THB"),
)
EVERY_UNITS = ("days", "months", "years")
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
        },
        {
            "external_id": "rapporto-90",
            "section": "immigrazione",
            "title": "Rapporto 90 giorni",
            "person": "",
            "due_day": None,
            "due_date": "2026-03-01",
            "interval": "days_90",
            "amount_thb": None,
            "notes": "",
            "status": "pending",
            "every": 90,
            "every_unit": "days",
        },
        {
            "external_id": "bollo-auto",
            "section": "garage",
            "title": "Bollo",
            "person": "",
            "due_day": None,
            "due_date": "2026-06-15",
            "interval": "yearly",
            "amount_thb": 1500,
            "notes": "",
            "status": "pending",
            "every": 1,
            "every_unit": "years",
            "category": "garage",
            "increase_threshold_pct": 15,
        },
    ],
    "cards": [
        {
            "external_id": "bbl-visa",
            "name": "Bangkok Bank",
            "type": "credito",
            "currency": "THB",
        },
        {
            "external_id": "kast",
            "name": "Kast Finance",
            "type": "debito",
            "month": "2026-10",
            "currency": "USD",
            "opening": 1000,
            "money_in": 200,
            "money_out": 150,
            "closing": 1050,
            "plastics": [{"last4": "4242", "label": "virtuale"}],
            "transactions": [
                {
                    "date": "2026-10-03",
                    "merchant": "Market",
                    "direction": "out",
                    "original_amount": 12.5,
                    "original_currency": "USD",
                    "statement_amount": 12.5,
                    "fx_rate": 32.8,
                    "last4": "4242",
                },
                {
                    "date": "2026-10-04",
                    "merchant": "Received",
                    "direction": "in",
                    "original_amount": 20,
                    "original_currency": "USD",
                    "statement_amount": 20,
                    "last4": "4242",
                },
            ],
        },
        {
            "external_id": "etherfi",
            "name": "Ether.fi",
            "type": "debito",
            "month": "2026-10",
            "currency": "USD",
            "money_in": 5,
            "money_out": 40,
            "plastics": [{"last4": "1881"}, {"last4": "1882"}],
        },
    ],
}
INTERVALS = ("monthly", "yearly", "once", "days_90")
STATUSES = ("pending", "paid", "done")


def today_bkk() -> date:
    return datetime.now(TZ).date()


def previous_month(today: date | None = None) -> tuple[int, int]:
    """The closed statement month in Asia/Bangkok. January opens December of the year before."""
    today = today or today_bkk()
    if today.month == 1:
        return today.year - 1, 12
    return today.year, today.month - 1


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


def add_months(start: date, months: int) -> date:
    index = start.month - 1 + months
    year = start.year + index // 12
    month = index % 12 + 1
    return month_due(year, month, start.day)


def add_years(start: date, years: int) -> date:
    return add_months(start, years * 12)


def shift_date(start: date, every: int, unit: str) -> date:
    if unit == "days":
        return start + timedelta(days=every)
    if unit == "months":
        return add_months(start, every)
    return add_years(start, every)


def is_monthly(section: str, payload: dict) -> bool:
    return payload.get("interval") == "monthly" or (
        section == "casa" and payload.get("giorno") and not payload.get("interval")
    )


def _has_due_date(payload: dict) -> bool:
    for key in ("due_date", "data", "scadenza", "scadenza_visto", "prossima", "ultimo_rapporto"):
        if payload.get(key):
            return True
    return False


def advance_payload(section: str, payload: dict, closed: date) -> dict | None:
    """Next occurrence after a completed period. Monthly bills stay on the same day."""
    if is_monthly(section, payload):
        return None
    every = payload.get("every")
    unit = payload.get("every_unit")
    tipo = payload.get("tipo")
    interval = payload.get("interval")
    if every and unit:
        new = shift_date(closed, int(every), str(unit))
    elif interval == "days_90" or tipo == "rapporto_90":
        new = closed + timedelta(days=90)
    elif interval == "yearly" or (tipo in {"visto", "bollo", "tagliando", "garanzia"} and _has_due_date(payload)):
        new = add_years(closed, 1)
    else:
        return None
    updated = dict(payload)
    iso = new.isoformat()
    if interval == "days_90" or tipo == "rapporto_90":
        updated["ultimo_rapporto"] = closed.isoformat()
        updated["prossima"] = iso
    if updated.get("due_date"):
        updated["due_date"] = iso
    if section == "garage" and updated.get("data"):
        updated["data"] = iso
    if section == "documenti" and updated.get("scadenza"):
        updated["scadenza"] = iso
    if updated.get("scadenza_visto"):
        updated["scadenza_visto"] = iso
    if updated.get("prossima") and interval != "days_90" and tipo != "rapporto_90":
        updated["prossima"] = iso
    if "status" in updated:
        updated["status"] = "pending"
    return updated


def _parse_amount(value) -> Decimal | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        dec = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not dec.is_finite():
        return None
    return dec


def _amount_text(dec: Decimal) -> str:
    text = format(dec, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _payload_amount(payload: dict) -> Decimal | None:
    if not isinstance(payload, dict):
        return None
    for key in ("amount_thb", "importo_thb"):
        parsed = _parse_amount(payload.get(key))
        if parsed is not None:
            return parsed
    return None


def _basis(section: str, payload: dict) -> str:
    interval = payload.get("interval")
    tipo = payload.get("tipo")
    if is_monthly(section, payload) or interval == "monthly":
        return "rispetto al mese scorso"
    if interval == "yearly" or tipo in {"visto", "bollo", "tagliando", "garanzia"}:
        return "rispetto all'anno scorso"
    return "rispetto al pagamento precedente"


def price_alert(section: str, payload: dict, history: list, today: date) -> dict | None:
    raw_threshold = payload.get("increase_threshold_pct")
    threshold = Decimal("15") if raw_threshold is None or raw_threshold == "" else Decimal(str(raw_threshold))
    payments: list[tuple[str, Decimal]] = []
    for entry in history:
        if _hist(entry, "kind") not in ("pagato", "fatto"):
            continue
        amount = _parse_amount(_hist(entry, "amount_thb"))
        if amount is None:
            continue
        stamp = _hist(entry, "paid_at") or _hist(entry, "created_at") or ""
        payments.append((str(stamp), amount))
    payments.sort()
    period = period_for(section, payload, today)
    done = any(
        _hist(entry, "period") == period and _hist(entry, "kind") in ("pagato", "fatto")
        for entry in history
    )
    if not done:
        current = _payload_amount(payload)
        if current is None or not payments:
            return None
        previous = payments[-1][1]
    else:
        if len(payments) < 2:
            return None
        previous = payments[-2][1]
        current = payments[-1][1]
    if previous <= 0 or current <= previous:
        return None
    pct = (current - previous) / previous * Decimal("100")
    if pct <= threshold:
        return None
    shown = int(pct.to_integral_value())
    if shown < 1:
        shown = 1
    label = _label(section, payload)
    basis = _basis(section, payload)
    return {
        "pct": shown,
        "badge": f"Rincaro +{shown}%",
        "text": f"{label} +{shown}% {basis}",
    }


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
            CREATE TABLE IF NOT EXISTS meta (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS cards (
              id TEXT PRIMARY KEY,
              name TEXT NOT NULL,
              kind TEXT NOT NULL,
              currency TEXT NOT NULL,
              external_id TEXT UNIQUE,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS card_months (
              card_id TEXT NOT NULL,
              month TEXT NOT NULL,
              amount_thb TEXT,
              currency TEXT NOT NULL,
              amount_original TEXT,
              rate_to_thb TEXT,
              note TEXT,
              updated_at TEXT NOT NULL,
              PRIMARY KEY (card_id, month)
            );
            CREATE TABLE IF NOT EXISTS card_plastics (
              id TEXT PRIMARY KEY,
              card_id TEXT NOT NULL,
              last4 TEXT NOT NULL,
              label TEXT,
              UNIQUE (card_id, last4)
            );
            CREATE TABLE IF NOT EXISTS card_tx (
              id TEXT PRIMARY KEY,
              card_id TEXT NOT NULL,
              month TEXT NOT NULL,
              tx_date TEXT,
              merchant TEXT,
              direction TEXT NOT NULL,
              original_amount TEXT,
              original_currency TEXT,
              statement_amount TEXT,
              statement_currency TEXT,
              fx_rate TEXT,
              last4 TEXT,
              section TEXT
            );
            """
        )
        _add_columns(conn, "card_months", ("opening", "money_in", "money_out", "closing"))
        _add_columns(conn, "card_tx", ("category",))
        _backfill_tx_categories(conn)
        _drop_probe_schema(conn)
        _ensure_canonical_cards(conn)


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


def _meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    if row is None:
        return default
    return row["value"]


def _set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


CARD_OUT_CATEGORIES = (
    ("Trasporti", ("grab", "taxi", "bolt")),
    ("Spesa", ("7-11", "7 eleven", "lotus", "makro", "gourmet", "big c", "foodland")),
    ("Ristoranti", ("kfc", "cremerie", "restaurant", "cafe", "bar")),
    ("Abbonamenti", ("netflix", "youtube", "apple", "spotify", "grok", "x corp", "elevenreader", "google")),
    ("Casa", ("ais", "pea", "pwa", "true", "electricity", "megahome", "homepro", "mr.diy", "xiaomi home")),
    ("Salute", ("pharmacy", "goodwill", "hospital", "clinic")),
    ("Acquisti", ("lazada", "amazon", "amz_")),
)
CARD_OUT_LABELS = tuple(name for name, _keys in CARD_OUT_CATEGORIES) + ("Altro",)
_CARD_OUT_CANONICAL = {name.lower(): name for name in CARD_OUT_LABELS}


def merchant_category(merchant: str) -> str:
    """First matching merchant rule. Short words do not match inside a longer word."""
    text = str(merchant or "").lower()
    for label, keys in CARD_OUT_CATEGORIES:
        for key in keys:
            if key.endswith("_"):
                if key in text:
                    return label
                continue
            if re.search(rf"(?<![a-z0-9]){re.escape(key)}(?![a-z0-9])", text):
                return label
    return "Altro"


def _stored_category(value) -> str | None:
    """A hand-set category stays. activity and a blank are not a category."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or text.lower() == "activity":
        return None
    return _CARD_OUT_CANONICAL.get(text.lower(), text)


def _tx_category(item: dict, remembered: str | None = None) -> str:
    kept = _stored_category(item.get("category"))
    if kept:
        return kept
    if remembered:
        return remembered
    return merchant_category(str(item.get("merchant") or ""))


def _backfill_tx_categories(conn: sqlite3.Connection) -> None:
    rows = conn.execute("SELECT id, merchant, category FROM card_tx").fetchall()
    for row in rows:
        if _stored_category(row["category"]):
            continue
        conn.execute(
            "UPDATE card_tx SET category = ? WHERE id = ?",
            (merchant_category(row["merchant"] or ""), row["id"]),
        )


def _add_columns(conn: sqlite3.Connection, table: str, columns: tuple[str, ...]) -> None:
    have = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    for name in columns:
        if name not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} TEXT")


def _ensure_canonical_cards(conn: sqlite3.Connection) -> None:
    """Give the three accounts stable ids. An existing row is updated, never copied."""
    fresh = _meta(conn, "cards_seeded") is None
    stamp = now_iso()
    for external_id, name, kind, currency in CANONICAL_CARDS:
        if conn.execute("SELECT 1 FROM cards WHERE external_id = ?", (external_id,)).fetchone():
            continue
        named = conn.execute(
            "SELECT id, external_id FROM cards WHERE name = ? ORDER BY created_at",
            (name,),
        ).fetchall()
        reusable = next((row for row in named if not row["external_id"]), None)
        if reusable is not None:
            conn.execute(
                "UPDATE cards SET external_id = ?, kind = ?, currency = ?, updated_at = ? WHERE id = ?",
                (external_id, kind, currency, stamp, reusable["id"]),
            )
            continue
        if not fresh:
            continue
        conn.execute(
            """
            INSERT INTO cards (id, name, kind, currency, external_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (str(uuid4()), name, kind, currency, external_id, stamp, stamp),
        )
    _set_meta(conn, "cards_seeded", "1")


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
        "rincaro": price_alert(section, payload, item["history"], today),
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
    rincari = [
        {
            "id": view["id"],
            "label": view["label"],
            "text": view["rincaro"]["text"],
            "badge": view["rincaro"]["badge"],
            "pct": view["rincaro"]["pct"],
        }
        for view in views
        if view.get("rincaro")
    ]
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
        "rincari": rincari,
        "spese": spending("mese", False),
        "spese_anno": spending("anno", False),
        "spese_con_carte": spending("mese", True),
        "spese_anno_con_carte": spending("anno", True),
        "carte": cards_summary(),
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
    every_present = "every" in raw and raw.get("every") not in (None, "")
    unit_present = "every_unit" in raw and raw.get("every_unit") not in (None, "")
    if every_present != unit_present:
        raise Invalid("every_unit" if every_present else "every")
    every = None
    every_unit = None
    if every_present:
        every = _whole(raw.get("every"), "every", 1, 10000, required=True)
        every_unit = raw.get("every_unit")
        if not isinstance(every_unit, str) or every_unit.strip().lower() not in EVERY_UNITS:
            raise Invalid("every_unit")
        every_unit = every_unit.strip().lower()
    category = None
    if "category" in raw and raw.get("category") not in (None, ""):
        if not isinstance(raw.get("category"), str):
            raise Invalid("category")
        category = raw["category"].strip().lower()
        if category == "carte" or category not in CATEGORIES:
            raise Invalid("category")
    threshold = None
    if "increase_threshold_pct" in raw and raw.get("increase_threshold_pct") not in (None, ""):
        parsed = _parse_amount(raw.get("increase_threshold_pct"))
        if parsed is None or parsed < 0 or parsed > 1000:
            raise Invalid("increase_threshold_pct")
        threshold = int(parsed) if parsed == parsed.to_integral_value() else float(parsed)
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
    if every is not None:
        payload["every"] = every
        payload["every_unit"] = every_unit
    if category:
        payload["category"] = category
    if threshold is not None:
        payload["increase_threshold_pct"] = threshold
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

    if not isinstance(body, dict):
        raise Invalid("items")
    has_items = "items" in body
    has_cards = "cards" in body
    if not has_items and not has_cards:
        raise Invalid("items")
    raw_items = body.get("items", [])
    if has_items and not isinstance(raw_items, list):
        raise Invalid("items")
    if not has_items:
        raw_items = []
    raw_cards = body.get("cards", [])
    if has_cards and not isinstance(raw_cards, list):
        raise Invalid("cards")
    if not has_cards:
        raw_cards = []
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
    cards_created: list[str] = []
    cards_updated: list[str] = []
    card_months = 0
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
        for raw in raw_cards:
            created_flag, months = _import_card(conn, raw, stamp)
            card_months += months
            if created_flag == "created":
                cards_created.append(raw.get("external_id") or raw.get("name") or "")
            elif created_flag == "updated":
                cards_updated.append(raw.get("external_id") or raw.get("name") or "")
    return {
        "created": created,
        "updated": updated,
        "cards_created": cards_created,
        "cards_updated": cards_updated,
        "card_months": card_months,
    }


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
    import json

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
    with _db_lock() as conn:
        found = None
        for item in _load(conn):
            if item["id"] == item_id:
                found = item
                break
        if found is None:
            return None
        paid_date = date.fromisoformat(paid_at_iso)
        if "paid_at" in body and body.get("paid_at"):
            anchor = paid_date
        else:
            anchor = today
        if is_monthly(found["section"], found["payload"]):
            period = f"{anchor.year:04d}-{anchor.month:02d}"
        elif "paid_at" in body and body.get("paid_at"):
            period = paid_at_iso
        else:
            period = period_for(found["section"], found["payload"], today)
        if "amount_thb" in body:
            amount = _amount(body.get("amount_thb"))
        else:
            copied = _payload_amount(found["payload"])
            amount = None if copied is None else _amount_text(copied)
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
        due = compute_due(found["section"], found["payload"], anchor)
        closed = due or paid_date
        nxt = advance_payload(found["section"], found["payload"], closed)
        if nxt is not None:
            conn.execute(
                "UPDATE items SET payload = ?, updated_at = ? WHERE id = ?",
                (json.dumps(nxt, ensure_ascii=False), now_iso(), item_id),
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
    return {"items": items, "history": history, "cards": list_cards()}


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
        "rincaro": view.get("rincaro"),
        "fields": view["payload"],
        "history": view["history"],
    }


def public_item(view: dict) -> dict:
    return _public_item(view)


def _reject_card_secrets(data: dict) -> None:
    if not isinstance(data, dict):
        raise Invalid("body")
    for key in FORBIDDEN_CARD_KEYS:
        value = data.get(key)
        if value not in (None, "", [], {}):
            raise Invalid(key)
    for key in ("name", "merchant", "note", "label"):
        value = data.get(key)
        if isinstance(value, str) and len("".join(ch for ch in value if ch.isdigit())) >= 12:
            raise Invalid(key)
    for child_key in ("transactions", "plastics"):
        children = data.get(child_key) or []
        if isinstance(children, list):
            for child in children:
                if isinstance(child, dict):
                    _reject_card_secrets(child)


def _last4(value, *, required: bool = False) -> str | None:
    if value in (None, ""):
        if required:
            raise Invalid("last4")
        return None
    text = str(value).strip()
    if len(text) != 4 or not text.isdigit():
        raise Invalid("last4")
    return text


def _optional_money(data: dict, key: str) -> str | None:
    if key not in data or data.get(key) in (None, ""):
        return None
    parsed = _parse_amount(data.get(key))
    if parsed is None or parsed < 0:
        raise Invalid(key)
    return _amount_text(parsed)


def _tx_key(row: dict) -> tuple:
    return (
        str(row.get("date") or row.get("tx_date") or ""),
        str(row.get("merchant") or "").strip().lower(),
        str(row.get("direction") or ""),
        str(row.get("statement_amount") or ""),
        str(row.get("last4") or ""),
    )


def _dedupe_transactions(rows: list[dict]) -> list[dict]:
    """Ether.fi lists the same spend under Activity and again under Pending."""
    activity = [row for row in rows if str(row.get("section") or "activity").lower() != "pending"]
    pending = [row for row in rows if str(row.get("section") or "").lower() == "pending"]
    seen = {_tx_key(row) for row in activity}
    kept = list(activity)
    for row in pending:
        key = _tx_key(row)
        if key in seen:
            continue
        kept.append(row)
        seen.add(key)
    return kept


def _is_spending_tx(row: dict) -> bool:
    merchant = str(row.get("merchant") or "").strip().lower()
    if "cash reward" in merchant or "cashback" in merchant or "cash back" in merchant:
        return False
    if merchant == "received" or merchant.startswith("received "):
        return False
    return str(row.get("direction") or "").lower() == "out"


def _native_label(amount: str | None, currency: str) -> str:
    if not amount:
        return ""
    if currency == "THB":
        return format_baht(amount)
    return f"{amount} {currency}"


def _month_text(value) -> str:
    if not isinstance(value, str):
        raise Invalid("month")
    text = value.strip()
    try:
        year, month = text.split("-")
        if len(year) != 4 or len(month) != 2:
            raise ValueError
        date(int(year), int(month), 1)
    except (ValueError, TypeError) as exc:
        raise Invalid("month") from exc
    return text


def _rate(conn: sqlite3.Connection, currency: str, given) -> Decimal | None:
    if currency == "THB":
        return Decimal("1")
    parsed = _parse_amount(given)
    if parsed is not None and parsed > 0:
        key = "rate_usd" if currency == "USD" else "rate_eur"
        _set_meta(conn, key, _amount_text(parsed))
        return parsed
    key = "rate_usd" if currency == "USD" else "rate_eur"
    stored = _parse_amount(_meta(conn, key))
    if stored is None or stored <= 0:
        raise Invalid("rate_to_thb")
    return stored


def _thb_from(conn: sqlite3.Connection, data: dict, currency: str) -> tuple[str, str | None, str | None]:
    if "amount_thb" in data and data.get("amount_thb") not in (None, ""):
        amount = _amount(data.get("amount_thb"))
        if amount is None:
            raise Invalid("amount_thb")
        return amount, currency, None
    original = _parse_amount(data.get("amount"))
    if original is None or original < 0:
        raise Invalid("amount")
    rate = _rate(conn, currency, data.get("rate_to_thb"))
    assert rate is not None
    thb = original if currency == "THB" else original * rate
    return _amount_text(thb), _amount_text(original), None if currency == "THB" else _amount_text(rate)


def _import_card(conn: sqlite3.Connection, raw: dict, stamp: str) -> tuple[str, int]:
    _reject_card_secrets(raw)
    if not isinstance(raw, dict):
        raise Invalid("cards")
    external_id = raw.get("external_id")
    if not isinstance(external_id, str) or not external_id.strip() or len(external_id.strip()) > 200:
        raise Invalid("external_id")
    external_id = external_id.strip()
    row = conn.execute("SELECT * FROM cards WHERE external_id = ?", (external_id,)).fetchone()
    name = raw.get("name")
    created_flag = "updated" if row else "created"
    if row is None:
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80:
            raise Invalid("name")
        kind = raw.get("type") or raw.get("kind")
        if not isinstance(kind, str) or kind.strip().lower() not in CARD_KINDS:
            raise Invalid("type")
        currency = str(raw.get("currency") or "THB").strip().upper()
        if currency not in CARD_CURRENCIES:
            raise Invalid("currency")
        card_id = str(uuid4())
        conn.execute(
            """
            INSERT INTO cards (id, name, kind, currency, external_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (card_id, name.strip(), kind.strip().lower(), currency, external_id, stamp, stamp),
        )
    else:
        card_id = row["id"]
        currency = row["currency"]
        if raw.get("currency"):
            currency = str(raw["currency"]).strip().upper()
            if currency not in CARD_CURRENCIES:
                raise Invalid("currency")
        kind = row["kind"]
        if raw.get("type") or raw.get("kind"):
            kind_raw = raw.get("type") or raw.get("kind")
            if not isinstance(kind_raw, str) or kind_raw.strip().lower() not in CARD_KINDS:
                raise Invalid("type")
            kind = kind_raw.strip().lower()
        new_name = row["name"]
        if isinstance(name, str) and name.strip():
            new_name = name.strip()
        conn.execute(
            "UPDATE cards SET name = ?, kind = ?, currency = ?, updated_at = ? WHERE id = ?",
            (new_name, kind, currency, stamp, card_id),
        )
    _save_plastics(conn, card_id, raw.get("plastics") or [])
    wrote = 0
    month_keys = ("opening", "money_in", "money_out", "closing", "transactions", "amount", "amount_thb")
    if raw.get("month") or any(raw.get(key) not in (None, "", []) for key in month_keys):
        if not raw.get("month"):
            raise Invalid("month")
        _write_month(conn, card_id, raw, stamp)
        wrote = 1
    return created_flag, wrote


def _save_plastics(conn: sqlite3.Connection, card_id: str, plastics) -> None:
    if not isinstance(plastics, list):
        raise Invalid("plastics")
    for item in plastics:
        if not isinstance(item, dict):
            raise Invalid("plastics")
        last4 = _last4(item.get("last4"), required=True)
        label = item.get("label") or ""
        if not isinstance(label, str) or len(label) > 40:
            raise Invalid("label")
        found = conn.execute(
            "SELECT id FROM card_plastics WHERE card_id = ? AND last4 = ?",
            (card_id, last4),
        ).fetchone()
        if found:
            conn.execute("UPDATE card_plastics SET label = ? WHERE id = ?", (label, found["id"]))
        else:
            conn.execute(
                "INSERT INTO card_plastics (id, card_id, last4, label) VALUES (?, ?, ?, ?)",
                (str(uuid4()), card_id, last4, label),
            )


def _supplied_money(raw: dict, key: str) -> bool:
    return key in raw and raw.get(key) not in (None, "")


def _write_month(conn: sqlite3.Connection, card_id: str, raw: dict, stamp: str) -> None:
    """Blank opening, in, out or closing stays null and does not replace a saved figure."""
    month = _month_text(raw.get("month"))
    card = conn.execute("SELECT currency FROM cards WHERE id = ?", (card_id,)).fetchone()
    existing = conn.execute(
        "SELECT * FROM card_months WHERE card_id = ? AND month = ?",
        (card_id, month),
    ).fetchone()
    currency = (existing["currency"] if existing else None) or (card["currency"] if card else "THB")
    if raw.get("currency"):
        currency = str(raw["currency"]).strip().upper()
        if currency not in CARD_CURRENCIES:
            raise Invalid("currency")

    def keep(key: str):
        if _supplied_money(raw, key):
            return _optional_money(raw, key)
        if existing is not None:
            return existing[key]
        return None

    opening = keep("opening")
    money_in = keep("money_in")
    money_out = keep("money_out")
    closing = keep("closing")
    if not _supplied_money(raw, "money_out") and raw.get("amount") not in (None, ""):
        parsed_amount = _parse_amount(raw.get("amount"))
        if parsed_amount is None or parsed_amount < 0:
            raise Invalid("amount")
        money_out = _amount_text(parsed_amount)
    elif (
        money_out is None
        and existing is None
        and raw.get("amount_thb") not in (None, "")
        and currency == "THB"
    ):
        money_out = _amount_text(_parse_amount(raw.get("amount_thb")))
    if "note" in raw and raw.get("note") is not None:
        note = raw.get("note") or ""
    else:
        note = existing["note"] if existing else ""
    if not isinstance(note, str) or len(note) > 500:
        raise Invalid("note")
    if existing is None:
        conn.execute(
            """
            INSERT INTO card_months (
              card_id, month, amount_thb, currency, amount_original, rate_to_thb, note, updated_at,
              opening, money_in, money_out, closing
            ) VALUES (?, ?, '', ?, NULL, NULL, ?, ?, ?, ?, ?, ?)
            """,
            (card_id, month, currency, note, stamp, opening, money_in, money_out, closing),
        )
    else:
        conn.execute(
            """
            UPDATE card_months
            SET currency = ?, note = ?, updated_at = ?,
                opening = ?, money_in = ?, money_out = ?, closing = ?
            WHERE card_id = ? AND month = ?
            """,
            (currency, note, stamp, opening, money_in, money_out, closing, card_id, month),
        )
    if "transactions" in raw:
        if not isinstance(raw.get("transactions"), list):
            raise Invalid("transactions")
        remembered: dict[tuple, str] = {}
        for row in conn.execute(
            "SELECT tx_date, merchant, direction, statement_amount, last4, category FROM card_tx WHERE card_id = ? AND month = ?",
            (card_id, month),
        ):
            kept = _stored_category(row["category"])
            if kept:
                remembered[_tx_key(dict(row))] = kept
        conn.execute("DELETE FROM card_tx WHERE card_id = ? AND month = ?", (card_id, month))
        for item in _dedupe_transactions(raw["transactions"]):
            _insert_tx(conn, card_id, month, currency, item, remembered.get(_tx_key(item)))


def _insert_tx(conn: sqlite3.Connection, card_id: str, month: str, currency: str, item: dict, remembered: str | None = None) -> None:
    if not isinstance(item, dict):
        raise Invalid("transactions")
    _reject_card_secrets(item)
    direction = str(item.get("direction") or "").strip().lower()
    if direction not in ("in", "out"):
        raise Invalid("direction")
    statement = _parse_amount(item.get("statement_amount"))
    if statement is None:
        statement = _parse_amount(item.get("original_amount"))
    if statement is None or statement < 0:
        raise Invalid("statement_amount")
    original = _parse_amount(item.get("original_amount"))
    original_currency = str(item.get("original_currency") or currency).strip().upper()
    if original_currency not in CARD_CURRENCIES:
        raise Invalid("original_currency")
    fx = _parse_amount(item.get("fx_rate")) if item.get("fx_rate") not in (None, "") else None
    if fx is not None and fx <= 0:
        raise Invalid("fx_rate")
    last4 = _last4(item.get("last4"))
    merchant = item.get("merchant") or ""
    if not isinstance(merchant, str) or len(merchant) > 120:
        raise Invalid("merchant")
    tx_date = item.get("date") or ""
    if tx_date:
        try:
            tx_date = date.fromisoformat(str(tx_date)).isoformat()
        except ValueError as exc:
            raise Invalid("date") from exc
    section = str(item.get("section") or "activity").strip().lower()
    category = _tx_category(item, remembered)
    conn.execute(
        """
        INSERT INTO card_tx (
          id, card_id, month, tx_date, merchant, direction, original_amount, original_currency,
          statement_amount, statement_currency, fx_rate, last4, section, category
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(uuid4()), card_id, month, tx_date, merchant, direction,
            None if original is None else _amount_text(original), original_currency,
            _amount_text(statement), currency, None if fx is None else _amount_text(fx), last4, section, category,
        ),
    )


def _month_public(row, transactions: list[dict]) -> dict:
    currency = row["currency"] or "THB"
    spend, baht, complete = _month_spend(row, transactions, currency)
    return {
        "month": row["month"],
        "currency": currency,
        "opening": row["opening"],
        "money_in": row["money_in"],
        "money_out": row["money_out"],
        "closing": row["closing"],
        "amount_thb": baht,
        "spend": spend,
        "baht_complete": complete,
        "display": _native_label(spend, currency),
        "note": row["note"] or "",
        "transactions": transactions,
    }


def _month_spend(row, transactions: list[dict], currency: str) -> tuple[str | None, str | None, bool]:
    """Spending in statement currency, plus baht only when every foreign row has its own rate."""
    spending_rows = [item for item in transactions if _is_spending_tx(item)]
    if spending_rows:
        native = Decimal("0")
        baht = Decimal("0")
        complete = True
        for item in spending_rows:
            amount = _parse_amount(item.get("statement_amount")) or Decimal("0")
            native += amount
            row_currency = item.get("statement_currency") or currency
            rate = _parse_amount(item.get("fx_rate"))
            if row_currency == "THB":
                baht += amount
            elif rate is not None:
                baht += amount * rate
            else:
                complete = False
        return _amount_text(native), (_amount_text(baht) if complete else None), complete
    money_out = _parse_amount(row["money_out"])
    if money_out is None:
        legacy = _parse_amount(row["amount_thb"])
        if legacy is None:
            return None, None, currency == "THB"
        if currency == "THB":
            return _amount_text(legacy), _amount_text(legacy), True
        return _amount_text(legacy), None, False
    if currency == "THB":
        return _amount_text(money_out), _amount_text(money_out), True
    return _amount_text(money_out), None, False


def list_cards() -> list[dict]:
    with _db_lock() as conn:
        cards = conn.execute("SELECT * FROM cards ORDER BY name").fetchall()
        months = conn.execute("SELECT * FROM card_months ORDER BY month").fetchall()
        plastics = conn.execute("SELECT * FROM card_plastics ORDER BY last4").fetchall()
        txs = conn.execute("SELECT * FROM card_tx ORDER BY tx_date, id").fetchall()
    tx_grouped: dict[tuple[str, str], list] = {}
    for row in txs:
        tx_grouped.setdefault((row["card_id"], row["month"]), []).append(
            {
                "date": row["tx_date"],
                "merchant": row["merchant"] or "",
                "direction": row["direction"],
                "original_amount": row["original_amount"],
                "original_currency": row["original_currency"],
                "statement_amount": row["statement_amount"],
                "statement_currency": row["statement_currency"],
                "fx_rate": row["fx_rate"],
                "last4": row["last4"],
                "section": row["section"],
                "category": row["category"] or "",
            }
        )
    month_grouped: dict[str, list] = {}
    for row in months:
        month_grouped.setdefault(row["card_id"], []).append(
            _month_public(row, tx_grouped.get((row["card_id"], row["month"]), []))
        )
    plastic_grouped: dict[str, list] = {}
    for row in plastics:
        plastic_grouped.setdefault(row["card_id"], []).append(
            {"last4": row["last4"], "label": row["label"] or ""}
        )
    out = []
    for row in cards:
        out.append(
            {
                "id": row["id"],
                "name": row["name"],
                "kind": row["kind"],
                "type": row["kind"],
                "currency": row["currency"],
                "external_id": row["external_id"],
                "months": month_grouped.get(row["id"], []),
                "plastics": plastic_grouped.get(row["id"], []),
            }
        )
    return out


def create_card(data: dict) -> dict:
    _reject_card_secrets(data)
    name = _text(data, "name", required=True, limit=80)
    if len("".join(ch for ch in name if ch.isdigit())) >= 12:
        raise Invalid("name")
    kind = data.get("type") or data.get("kind")
    if not isinstance(kind, str) or kind.strip().lower() not in CARD_KINDS:
        raise Invalid("type")
    currency = str(data.get("currency") or "THB").strip().upper()
    if currency not in CARD_CURRENCIES:
        raise Invalid("currency")
    external_id = data.get("external_id")
    if external_id in (None, ""):
        external_id = None
    elif not isinstance(external_id, str):
        raise Invalid("external_id")
    else:
        external_id = external_id.strip()
    stamp = now_iso()
    card_id = str(uuid4())
    with _db_lock() as conn:
        if external_id:
            clash = conn.execute("SELECT 1 FROM cards WHERE external_id = ?", (external_id,)).fetchone()
            if clash:
                raise Invalid("external_id")
        conn.execute(
            """
            INSERT INTO cards (id, name, kind, currency, external_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (card_id, name, kind.strip().lower(), currency, external_id, stamp, stamp),
        )
    found = next(card for card in list_cards() if card["id"] == card_id)
    return found


def patch_card(card_id: str, data: dict) -> dict | None:
    _reject_card_secrets(data)
    with _db_lock() as conn:
        row = conn.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()
        if row is None:
            return None
        name = row["name"]
        if "name" in data:
            name = _text(data, "name", required=True, limit=80)
            if len("".join(ch for ch in name if ch.isdigit())) >= 12:
                raise Invalid("name")
        kind = row["kind"]
        if "type" in data or "kind" in data:
            kind_raw = data.get("type") or data.get("kind")
            if not isinstance(kind_raw, str) or kind_raw.strip().lower() not in CARD_KINDS:
                raise Invalid("type")
            kind = kind_raw.strip().lower()
        currency = row["currency"]
        if "currency" in data and data.get("currency") not in (None, ""):
            currency = str(data["currency"]).strip().upper()
            if currency not in CARD_CURRENCIES:
                raise Invalid("currency")
        conn.execute(
            "UPDATE cards SET name = ?, kind = ?, currency = ?, updated_at = ? WHERE id = ?",
            (name, kind, currency, now_iso(), card_id),
        )
    return next(card for card in list_cards() if card["id"] == card_id)


def delete_card(card_id: str) -> bool:
    with _db_lock() as conn:
        row = conn.execute("SELECT 1 FROM cards WHERE id = ?", (card_id,)).fetchone()
        if row is None:
            return False
        conn.execute("DELETE FROM card_tx WHERE card_id = ?", (card_id,))
        conn.execute("DELETE FROM card_plastics WHERE card_id = ?", (card_id,))
        conn.execute("DELETE FROM card_months WHERE card_id = ?", (card_id,))
        conn.execute("DELETE FROM cards WHERE id = ?", (card_id,))
    return True


def save_card_month(card_id: str, data: dict) -> dict | None:
    _reject_card_secrets(data)
    stamp = now_iso()
    with _db_lock() as conn:
        row = conn.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()
        if row is None:
            return None
        payload = dict(data)
        if not payload.get("currency"):
            payload["currency"] = row["currency"]
        if payload.get("amount") not in (None, "") and payload.get("money_out") in (None, ""):
            payload["money_out"] = payload.get("amount")
        _write_month(conn, card_id, payload, stamp)
    return next(card for card in list_cards() if card["id"] == card_id)


def delete_card_month(card_id: str, month: str) -> bool:
    month = _month_text(month)
    with _db_lock() as conn:
        conn.execute("DELETE FROM card_tx WHERE card_id = ? AND month = ?", (card_id, month))
        cur = conn.execute("DELETE FROM card_months WHERE card_id = ? AND month = ?", (card_id, month))
        return cur.rowcount > 0


def set_rates(usd, eur) -> None:
    with _db_lock() as conn:
        if usd not in (None, ""):
            parsed = _parse_amount(usd)
            if parsed is None or parsed <= 0:
                raise Invalid("rate_usd")
            _set_meta(conn, "rate_usd", _amount_text(parsed))
        if eur not in (None, ""):
            parsed = _parse_amount(eur)
            if parsed is None or parsed <= 0:
                raise Invalid("rate_eur")
            _set_meta(conn, "rate_eur", _amount_text(parsed))


def get_rates() -> dict[str, str]:
    with _db_lock() as conn:
        return {
            "rate_usd": _meta(conn, "rate_usd") or "",
            "rate_eur": _meta(conn, "rate_eur") or "",
        }


def item_category(section: str, payload: dict) -> str:
    raw = payload.get("category") if isinstance(payload, dict) else None
    if isinstance(raw, str) and raw.strip().lower() in CATEGORIES:
        return raw.strip().lower()
    if section == "casa":
        return "bollette"
    if section == "garage":
        return "garage"
    if section in ("documenti", "immigrazione"):
        return "documenti"
    return "altro"


def _in_period(stamp: str, periodo: str, today: date) -> bool:
    """mese is the previous Bangkok month. anno runs from January through today."""
    if not stamp or len(stamp) < 7:
        return False
    if periodo == "anno":
        if not stamp.startswith(f"{today.year:04d}"):
            return False
        if len(stamp) >= 10:
            return stamp[:10] <= today.isoformat()
        return stamp[:7] <= f"{today.year:04d}-{today.month:02d}"
    year, month = previous_month(today)
    return stamp.startswith(f"{year:04d}-{month:02d}")


def _card_spend_lines(conn: sqlite3.Connection, periodo: str, today: date) -> list[dict]:
    """One line per card and currency. Two dollar cards are not added together."""
    cards = conn.execute("SELECT id, name, currency FROM cards ORDER BY name").fetchall()
    months = conn.execute("SELECT * FROM card_months").fetchall()
    txs = conn.execute("SELECT * FROM card_tx").fetchall()
    grouped: dict[tuple[str, str], list] = {}
    for row in txs:
        grouped.setdefault((row["card_id"], row["month"]), []).append(dict(row))
    names = {row["id"]: row["name"] for row in cards}
    totals: dict[tuple[str, str], Decimal] = {}
    for row in months:
        if not _in_period(row["month"], periodo, today):
            continue
        currency = row["currency"] or "THB"
        spend, _baht, _complete = _month_spend(row, grouped.get((row["card_id"], row["month"]), []), currency)
        if not spend or _parse_amount(spend) == 0:
            continue
        key = (row["card_id"], currency)
        totals[key] = totals.get(key, Decimal("0")) + (_parse_amount(spend) or Decimal("0"))
    lines = []
    for card in cards:
        for (card_id, currency), amount in totals.items():
            if card_id != card["id"]:
                continue
            text = _amount_text(amount)
            lines.append({
                "id": card_id,
                "name": names.get(card_id) or "",
                "currency": currency,
                "amount": text,
                "baht": text if currency == "THB" else None,
                "baht_complete": currency == "THB",
                "display": _native_label(text, currency),
            })
    return lines


def spending(periodo: str, include_cards: bool) -> dict:
    if periodo not in ("mese", "anno"):
        raise Invalid("periodo")
    today = today_bkk()
    totals: dict[str, Decimal] = {}
    with _db_lock() as conn:
        items = _load(conn)
        for item in items:
            cat = item_category(item["section"], item["payload"])
            for entry in item["history"]:
                if entry["kind"] not in ("pagato", "fatto"):
                    continue
                if not _in_period(entry["paid_at"] or "", periodo, today):
                    continue
                amount = _parse_amount(entry["amount_thb"])
                if amount is None or amount <= 0:
                    continue
                totals[cat] = totals.get(cat, Decimal("0")) + amount
        card_lines: list[dict] = []
        if include_cards:
            card_lines = _card_spend_lines(conn, periodo, today)
    order = list(CATEGORIES)
    slices = []
    grand = sum(totals.values(), Decimal("0"))
    for key in order:
        amount = totals.get(key)
        if not amount:
            continue
        share = float(amount / grand * 100) if grand else 0
        slices.append(
            {
                "id": key,
                "label": CATEGORY_LABELS[key],
                "amount_thb": _amount_text(amount),
                "share": round(share, 1),
            }
        )
    return {
        "periodo": periodo,
        "carte": bool(include_cards),
        "total_thb": _amount_text(grand) if grand else None,
        "slices": slices,
        "card_lines": card_lines if include_cards else [],
        "empty": not slices and not (include_cards and card_lines),
    }


def _join_labels(parts: list[str]) -> str:
    return " · ".join(part for part in parts if part) or "—"


def cards_summary() -> dict:
    """One line per card and currency. No combined dollar total and no baht conversion."""
    today = today_bkk()
    month = f"{today.year:04d}-{today.month:02d}"
    year = f"{today.year:04d}"
    cards = list_cards()
    buckets: dict[tuple[str, str], dict] = {}
    for card in cards:
        for row in card["months"]:
            amount = _parse_amount(row.get("spend"))
            if not amount:
                continue
            currency = row.get("currency") or card["currency"]
            slot = buckets.setdefault(
                (card["id"], currency),
                {"id": card["id"], "name": card["name"], "currency": currency, "month": Decimal("0"), "year": Decimal("0"), "month_any": False, "year_any": False},
            )
            if row["month"] == month:
                slot["month"] += amount
                slot["month_any"] = True
            if str(row["month"]).startswith(year):
                slot["year"] += amount
                slot["year_any"] = True
    lines = []
    for card in cards:
        for (card_id, currency), slot in buckets.items():
            if card_id != card["id"]:
                continue
            month_text = _amount_text(slot["month"]) if slot["month_any"] else None
            year_text = _amount_text(slot["year"]) if slot["year_any"] else None
            lines.append({
                "id": card_id,
                "name": slot["name"],
                "currency": currency,
                "month": month_text,
                "year": year_text,
                "month_label": _native_label(month_text, currency) if month_text else "",
                "year_label": _native_label(year_text, currency) if year_text else "",
            })
    return {
        "count": len(cards),
        "lines": lines,
        "month_thb": None,
        "year_thb": None,
        "month_label": None,
        "year_label": None,
        "names": [card["name"] for card in cards],
    }


def card_chart(periodo: str = "anno") -> list[dict]:
    """Separate series per currency. A bar is one card, never a mix of baht and dollars."""
    today = today_bkk()
    year = f"{today.year:04d}"
    closed_year, closed_month = previous_month(today)
    month_key = f"{closed_year:04d}-{closed_month:02d}"
    cards = list_cards()
    series: dict[str, dict[str, list]] = {}
    for card in cards:
        for row in card["months"]:
            if periodo == "mese":
                if row["month"] != month_key:
                    continue
            elif not str(row["month"]).startswith(year) or str(row["month"]) > f"{year}-{today.month:02d}":
                continue
            amount = _parse_amount(row.get("spend"))
            if not amount:
                continue
            currency = row.get("currency") or card["currency"]
            text = _amount_text(amount)
            series.setdefault(currency, {}).setdefault(row["month"], []).append({
                "name": card["name"],
                "amount": text,
                "display": _native_label(text, currency),
            })
    out = []
    for currency in ("THB", "USD", "EUR"):
        months = series.get(currency)
        if not months:
            continue
        out.append({
            "currency": currency,
            "months": [
                {"month": month, "parts": months[month]}
                for month in sorted(months)
            ],
        })
    return out


def _is_chart_out(row: dict) -> bool:
    if not _is_spending_tx(row):
        return False
    merchant = str(row.get("merchant") or "").lower()
    return "saldo precedente" not in merchant and "previous balance" not in merchant


def _percent_bars(amounts: dict[str, Decimal]) -> list[dict]:
    total = sum(amounts.values(), Decimal("0"))
    if total <= 0:
        return []
    altro = Decimal("0")
    sized: list[tuple[str, Decimal]] = []
    for name, amount in amounts.items():
        if name == "Altro" or amount / total * 100 < 3:
            altro += amount
        else:
            sized.append((name, amount))
    sized.sort(key=lambda item: (-item[1], item[0]))
    if len(sized) > 7:
        altro += sum(amount for _name, amount in sized[7:])
        sized = sized[:7]
    if altro > 0:
        sized.append(("Altro", altro))
    sized.sort(key=lambda item: (-item[1], item[0]))
    bars = []
    for name, amount in sized[:8]:
        pct = int((amount / total * Decimal(100)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        bars.append({"category": name, "percent": pct})
    return bars


def card_out_charts(periodo: str) -> list[dict]:
    """Outflow share per currency. A foreign rate never merges two charts."""
    if periodo not in ("mese", "anno"):
        raise Invalid("periodo")
    today = today_bkk()
    with _db_lock() as conn:
        rows = conn.execute("SELECT * FROM card_tx").fetchall()
    buckets: dict[str, dict[str, Decimal]] = {}
    for row in rows:
        item = dict(row)
        if not _is_chart_out(item):
            continue
        stamp = str(item.get("tx_date") or "") or str(item.get("month") or "")
        if not _in_period(stamp, periodo, today):
            continue
        currency = str(item.get("statement_currency") or "THB").upper()
        amount = _parse_amount(item.get("statement_amount")) or Decimal("0")
        if amount <= 0:
            continue
        category = _stored_category(item.get("category")) or merchant_category(str(item.get("merchant") or ""))
        slot = buckets.setdefault(currency, {})
        slot[category] = slot.get(category, Decimal("0")) + amount
    charts = []
    for currency in ("THB", "USD", "EUR"):
        bars = _percent_bars(buckets.get(currency) or {})
        if bars:
            charts.append({"currency": currency, "bars": bars})
    return charts


def card_out_merchants(periodo: str, currency: str, category: str) -> list[dict]:
    """Merchants in one category, one currency, one period. Counts, not receipts."""
    if periodo not in ("mese", "anno"):
        raise Invalid("periodo")
    currency = str(currency or "").upper()
    wanted = _stored_category(category) or category
    today = today_bkk()
    with _db_lock() as conn:
        rows = conn.execute("SELECT * FROM card_tx").fetchall()
    grouped: dict[str, dict] = {}
    for row in rows:
        item = dict(row)
        if not _is_chart_out(item):
            continue
        stamp = str(item.get("tx_date") or "") or str(item.get("month") or "")
        if not _in_period(stamp, periodo, today):
            continue
        if str(item.get("statement_currency") or "").upper() != currency:
            continue
        label = _stored_category(item.get("category")) or merchant_category(str(item.get("merchant") or ""))
        if label != wanted:
            continue
        name = str(item.get("merchant") or "").strip() or "Altro"
        slot = grouped.setdefault(name.lower(), {"name": name, "count": 0})
        slot["count"] += 1
    return sorted(grouped.values(), key=lambda item: (-item["count"], item["name"].lower()))
