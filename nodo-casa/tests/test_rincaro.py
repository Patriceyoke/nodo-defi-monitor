"""A rise above the threshold is an alert. The first payment is not."""
from __future__ import annotations

import json
import sqlite3


def _insert(logic, external_id, payload, history):
    conn = sqlite3.connect(logic.DB_PATH)
    try:
        conn.execute(
            "INSERT INTO items (id, external_id, section, payload, created_at, updated_at) VALUES (?, ?, 'casa', ?, ?, ?)",
            (external_id, external_id, json.dumps(payload), "2026-01-01T00:00:00+07:00", "2026-01-01T00:00:00+07:00"),
        )
        for period, paid, amount in history:
            conn.execute(
                "INSERT INTO history (item_id, kind, period, paid_at, note, amount_thb, created_at) VALUES (?, 'pagato', ?, ?, '', ?, ?)",
                (external_id, period, paid, amount, paid + "T00:00:00+07:00"),
            )
        conn.commit()
    finally:
        conn.close()


def test_monthly_rise_yearly_wording_and_silence(casa):
    logic, server = casa
    today = logic.today_bkk()
    month = f"{today.year:04d}-{today.month:02d}"
    base = {
        "title": "Luce", "titolo": "Luce", "giorno": 10, "due_day": 10,
        "due_date": "", "interval": "monthly", "amount_thb": 1220, "status": "pending",
    }
    _insert(logic, "luce", base, [(month, today.isoformat(), "1220"), ("2026-01", "2026-01-10", "1000")])
    view = logic.get_item("luce")
    assert view["rincaro"]["badge"] == "Rincaro +22%"
    assert view["rincaro"]["text"] == "Luce +22% rispetto al mese scorso"
    html = server.page_home()
    assert "Luce +22% rispetto al mese scorso" in html
    assert "Rincaro +22%" in server.page_section("casa")
    assert "Luce +22% rispetto al mese scorso" in server.page_item(view)

    _insert(logic, "prima", {**base, "title": "Prima", "titolo": "Prima", "amount_thb": 1000}, [])
    logic.mark_done("prima", {"amount_thb": 1000})
    assert logic.get_item("prima")["rincaro"] is None

    _insert(logic, "uguale", {**base, "title": "Uguale", "titolo": "Uguale"}, [
        (month, today.isoformat(), "1000"), ("2026-01", "2026-01-10", "1000"),
    ])
    assert logic.get_item("uguale")["rincaro"] is None

    quiet = {**base, "title": "Piano", "titolo": "Piano", "amount_thb": 1100, "increase_threshold_pct": 15}
    _insert(logic, "piano", quiet, [(month, today.isoformat(), "1100"), ("2026-01", "2026-01-10", "1000")])
    assert logic.get_item("piano")["rincaro"] is None

    yearly = {
        "title": "Bollo", "titolo": "Bollo", "interval": "yearly", "due_date": "2026-06-15",
        "due_day": None, "amount_thb": 200, "status": "pending", "section": "garage",
    }
    conn = sqlite3.connect(logic.DB_PATH)
    conn.execute(
        "INSERT INTO items (id, external_id, section, payload, created_at, updated_at) VALUES ('bollo-r', 'bollo-r', 'garage', ?, ?, ?)",
        (json.dumps({**yearly, "veicolo": "Bollo", "tipo": "bollo", "data": "2026-06-15"}), "2026-01-01T00:00:00+07:00", "2026-01-01T00:00:00+07:00"),
    )
    conn.execute(
        "INSERT INTO history (item_id, kind, period, paid_at, note, amount_thb, created_at) VALUES ('bollo-r', 'fatto', '2026-06-15', '2026-06-15', '', '200', '2026-06-15T00:00:00+07:00')"
    )
    conn.execute(
        "INSERT INTO history (item_id, kind, period, paid_at, note, amount_thb, created_at) VALUES ('bollo-r', 'fatto', '2025-06-15', '2025-06-15', '', '100', '2025-06-15T00:00:00+07:00')"
    )
    conn.commit()
    conn.close()
    assert logic.get_item("bollo-r")["rincaro"]["text"] == "Bollo +100% rispetto all'anno scorso"
    home = logic.home()
    assert any(row["text"].startswith("Luce +22%") for row in home["rincari"])


def test_unpaid_compares_expected_amount_with_the_last_payment(casa):
    logic, _server = casa
    payload = {
        "title": "Acqua", "titolo": "Acqua", "giorno": 5, "due_day": 5, "due_date": "",
        "interval": "monthly", "amount_thb": 150, "status": "pending",
    }
    _insert(logic, "acqua", payload, [("2026-01", "2026-01-05", "100")])
    alert = logic.get_item("acqua")["rincaro"]
    assert alert["text"] == "Acqua +50% rispetto al mese scorso"
