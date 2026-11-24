"""Due dates move forward after Segna pagato / Segna fatto. History stays."""
from __future__ import annotations

import json
import sqlite3


def _insert(logic, external_id, section, payload):
    conn = sqlite3.connect(logic.DB_PATH)
    try:
        conn.execute(
            "INSERT INTO items (id, external_id, section, payload, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (external_id, external_id, section, json.dumps(payload), "2026-01-01T00:00:00+07:00", "2026-01-01T00:00:00+07:00"),
        )
        conn.commit()
    finally:
        conn.close()
    return external_id


def _payload(logic, external_id):
    conn = sqlite3.connect(logic.DB_PATH)
    try:
        raw = conn.execute("SELECT payload FROM items WHERE external_id = ?", (external_id,)).fetchone()[0]
    finally:
        conn.close()
    return json.loads(raw)


def _history(logic, external_id):
    conn = sqlite3.connect(logic.DB_PATH)
    try:
        return conn.execute(
            "SELECT kind, period, paid_at, amount_thb FROM history WHERE item_id = ?",
            (external_id,),
        ).fetchall()
    finally:
        conn.close()


def test_monthly_stays_on_the_same_day_and_keeps_history(casa):
    logic, _server = casa
    _insert(logic, "luce", "casa", {
        "title": "Luce", "titolo": "Luce", "giorno": 31, "due_day": 31,
        "due_date": "", "interval": "monthly", "amount_thb": 1000, "importo_thb": "1000",
        "status": "pending",
    })
    done = logic.mark_done("luce", {})
    assert done["done"] is True
    assert _payload(logic, "luce")["giorno"] == 31
    row = _history(logic, "luce")[0]
    assert row[0] == "pagato"
    assert row[3] == "1000"


def test_yearly_and_short_month_and_ninety_days(casa):
    logic, _server = casa
    assert logic.add_months(logic.date(2026, 1, 31), 1).isoformat() == "2026-02-28"
    assert logic.add_months(logic.date(2024, 1, 31), 1).isoformat() == "2024-02-29"
    _insert(logic, "bollo", "garage", {
        "title": "Bollo", "veicolo": "Bollo", "tipo": "bollo", "data": "2026-06-15",
        "due_date": "2026-06-15", "interval": "yearly", "status": "pending",
    })
    logic.mark_done("bollo", {"paid_at": "2026-06-20"})
    moved = _payload(logic, "bollo")
    assert moved["data"] == "2027-06-15"
    assert moved["due_date"] == "2027-06-15"
    assert moved["status"] == "pending"
    hist = _history(logic, "bollo")[0]
    assert hist[1] == "2026-06-20"
    assert hist[2] == "2026-06-20"
    assert logic.get_item("bollo")["done"] is False

    _insert(logic, "fine", "casa", {
        "title": "Fine mese", "titolo": "Fine mese", "due_date": "2026-01-31",
        "interval": "once", "every": 1, "every_unit": "months", "amount_thb": 10, "status": "pending",
    })
    logic.mark_done("fine", {})
    assert _payload(logic, "fine")["due_date"] == "2026-02-28"

    _insert(logic, "rep", "immigrazione", {
        "title": "Rapporto", "tipo": "rapporto_90", "prossima": "2026-03-01",
        "due_date": "2026-03-01", "interval": "days_90", "status": "pending",
    })
    logic.mark_done("rep", {})
    report = _payload(logic, "rep")
    assert report["ultimo_rapporto"] == "2026-03-01"
    assert report["prossima"] == "2026-05-30"
    assert logic.get_item("rep")["stato"] == "da_fare"


def test_once_without_every_does_not_move(casa):
    logic, _server = casa
    _insert(logic, "una", "documenti", {
        "title": "Una volta", "tipo": "Una volta", "persona": "", "scadenza": "2026-08-01",
        "due_date": "2026-08-01", "interval": "once", "status": "pending",
    })
    logic.mark_done("una", {})
    assert _payload(logic, "una")["scadenza"] == "2026-08-01"
    assert logic.get_item("una")["done"] is True
