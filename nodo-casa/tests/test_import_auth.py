"""Bearer rules for import and patch. The socket peer is the only address that counts."""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout
from io import BytesIO
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))

_TMP = Path(tempfile.mkdtemp(prefix="casa-auth-"))
os.environ["DATA_DIR"] = str(_TMP)

import logic  # noqa: E402
import server  # noqa: E402

logic.DATA_DIR = _TMP
logic.DB_PATH = _TMP / "casa.db"
logic.TOKEN_PATH = _TMP / "agent.token"
logic.ensure_storage()


class Headers(dict):
    def get(self, key, default=""):
        return super().get(key, default)


def call(method: str, path: str, ip: str, body: dict | None, bearer: str | None, extra: dict | None = None):
    raw = b"" if body is None else json.dumps(body).encode()
    handler = server.Handler.__new__(server.Handler)
    handler.client_address = (ip, 40000)
    handler.headers = Headers({"Content-Length": str(len(raw))})
    if bearer is not None:
        handler.headers["Authorization"] = bearer
    if extra:
        handler.headers.update(extra)
    handler.rfile = BytesIO(raw)
    handler.wfile = BytesIO()
    handler.path = path
    handler.command = method
    handler.requestline = f"{method} {path} HTTP/1.1"
    handler.request_version = "HTTP/1.1"
    handler.close_connection = True
    if method == "POST":
        handler.do_POST()
    elif method == "PATCH":
        handler.do_PATCH()
    else:
        handler.do_GET()
    text = handler.wfile.getvalue().decode()
    status = int(text.split(" ", 2)[1])
    payload = {}
    if "\r\n\r\n" in text:
        tail = text.split("\r\n\r\n", 1)[1]
        if tail:
            payload = json.loads(tail)
    return status, payload


def item_count() -> int:
    import sqlite3

    conn = sqlite3.connect(logic.DB_PATH)
    try:
        return conn.execute("select count(*) from items").fetchone()[0]
    finally:
        conn.close()


def test_network_bounds():
    assert server.peer_on_umbrel_docker("10.21.0.15")
    assert server.peer_on_umbrel_docker("10.21.0.0")
    assert server.peer_on_umbrel_docker("10.21.255.255")
    assert server.peer_on_umbrel_docker("172.16.0.1")
    assert server.peer_on_umbrel_docker("172.17.0.2")
    assert server.peer_on_umbrel_docker("172.31.255.254")
    assert server.peer_on_umbrel_docker("::ffff:10.21.0.15")
    assert not server.peer_on_umbrel_docker("10.20.255.255")
    assert not server.peer_on_umbrel_docker("10.22.0.1")
    assert not server.peer_on_umbrel_docker("172.15.255.255")
    assert not server.peer_on_umbrel_docker("172.32.0.1")
    assert not server.peer_on_umbrel_docker("192.168.1.10")
    assert not server.peer_on_umbrel_docker("127.0.0.1")
    assert not server.peer_on_umbrel_docker("")


def _bill(external_id: str, title: str, due_day: int) -> dict:
    return {
        "items": [
            {
                "external_id": external_id,
                "section": "casa",
                "title": title,
                "person": "",
                "due_day": due_day,
                "due_date": "",
                "interval": "monthly",
                "amount_thb": None,
                "notes": "",
                "status": "pending",
            }
        ]
    }


def test_import_and_patch_follow_the_socket_peer():
    token = logic.TOKEN_PATH.read_text(encoding="ascii").strip()
    body = _bill("ext-a", "Voce", 3)
    before = item_count()
    buf = io.StringIO()
    with redirect_stdout(buf):
        status, _ = call(
            "POST",
            "/api/import",
            "203.0.113.8",
            body,
            None,
            {"X-Forwarded-For": "10.21.0.15"},
        )
        assert status == 401
        assert item_count() == before

        status, payload = call(
            "POST",
            "/api/import",
            "10.21.0.15",
            body,
            None,
            {"X-Forwarded-For": "203.0.113.8"},
        )
        assert status == 200
        assert payload["created"]
        assert item_count() == before + 1

        other = {
            "items": [
                {
                    "external_id": "ext-b",
                    "section": "garage",
                    "title": "Voce",
                    "person": "",
                    "due_day": None,
                    "due_date": "2026-12-01",
                    "interval": "once",
                    "amount_thb": None,
                    "notes": "",
                    "status": "pending",
                }
            ]
        }
        status, _ = call("POST", "/api/import", "172.17.0.2", other, None)
        assert status == 200
        assert item_count() == before + 2

        status, _ = call("POST", "/api/import", "172.32.0.4", other, None)
        assert status == 401
        status, _ = call("POST", "/api/import", "10.22.0.1", other, None)
        assert status == 401
        assert item_count() == before + 2

        bad = _bill("ext-c", "Altra", 4)
        status, _ = call("POST", "/api/import", "10.21.0.15", bad, "Bearer sbagliato")
        assert status == 401
        status, _ = call("POST", "/api/import", "8.8.8.8", bad, "Bearer sbagliato")
        assert status == 401
        assert item_count() == before + 2

        update = _bill("ext-a", "Voce aggiornata", 9)
        status, payload = call("POST", "/api/import", "198.51.100.4", update, f"Bearer {token}")
        assert status == 200
        assert payload["updated"]
        assert payload["created"] == []
        assert item_count() == before + 2

    out = buf.getvalue()
    assert token not in out
    assert "Altra" not in out
    exported = logic.export_all()["items"]
    titles = [item["fields"]["title"] for item in exported if item["external_id"] == "ext-a"]
    assert titles == ["Voce aggiornata"]
    assert any(item["external_id"] == "ext-b" for item in exported)

    item_id = next(item["id"] for item in exported if item["external_id"] == "ext-b")
    status, _ = call("PATCH", f"/api/items/{item_id}", "127.0.0.1", {"note": "no"}, None)
    assert status == 401
    status, payload = call("PATCH", f"/api/items/{item_id}", "172.31.1.8", {"note": "si"}, None)
    assert status == 200
    assert payload["fields"]["note"] == "si"
    status, _ = call("PATCH", f"/api/items/{item_id}", "10.21.9.9", {"note": "no"}, "Bearer no")
    assert status == 401
    assert logic.get_item(item_id)["payload"]["note"] == "si"


def test_exact_import_schema_colors_and_cleanup():
    example = {
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
    status, schema = call("GET", "/api/schema", "10.21.0.15", None, None)
    assert status == 200
    assert schema == logic.IMPORT_EXAMPLE

    before = {item["external_id"] for item in logic.export_all()["items"]}
    status, payload = call("POST", "/api/import", "10.21.0.15", example, None)
    assert status == 200
    assert payload["created"] and payload["updated"] == []
    status, again = call("POST", "/api/import", "10.21.0.15", example, None)
    assert status == 200
    assert again["created"] == [] and again["updated"]
    ids = {item["external_id"] for item in logic.export_all()["items"]}
    assert "pwa-acqua" in ids
    assert before <= ids

    today = logic.today_bkk()
    due = logic.month_due(today.year, today.month, 10)
    acqua = next(item for item in logic.section_items("casa") if item["label"] == "Acqua PWA")
    assert acqua["color"] == logic.color_for((due - today).days, False)
    assert acqua["stato"] == "da_pagare"
    home_html = server.page_home()
    assert "Acqua PWA" in home_html
    assert "pwa-acqua" not in home_html
    assert "Acqua PWA" in server.page_section("casa")

    doc = {
        "items": [
            {
                "external_id": "doc-1",
                "section": "documenti",
                "title": "Tessera",
                "person": "",
                "due_day": None,
                "due_date": "2027-06-01",
                "interval": "yearly",
                "amount_thb": None,
                "notes": "",
                "status": "pending",
            }
        ]
    }
    status, _ = call("POST", "/api/import", "172.16.0.8", doc, None)
    assert status == 200
    docs = logic.section_items("documenti")
    assert docs[0]["label"] == "Tessera"
    assert docs[0]["color"] == "green"
    stay = call("POST", "/api/import", "10.21.0.15", example, None)
    assert any(item["external_id"] == "doc-1" for item in logic.export_all()["items"])
    assert stay[0] == 200

    report = {
        "items": [
            {
                "external_id": "rep-90",
                "section": "immigrazione",
                "title": "Rapporto",
                "person": "",
                "due_day": None,
                "due_date": "2026-11-25",
                "interval": "days_90",
                "amount_thb": None,
                "notes": "",
                "status": "pending",
            }
        ]
    }
    status, _ = call("POST", "/api/import", "10.21.0.15", report, None)
    assert status == 200
    seen = logic.section_items("immigrazione")
    assert seen[0]["label"] == "Rapporto"
    assert seen[0]["due"] == "2026-11-25"

    kept = item_count()
    base = dict(example["items"][0])
    checks = {
        "section": "altro",
        "title": "",
        "person": None,
        "due_day": 0,
        "due_date": "2026-13-01",
        "interval": "weekly",
        "amount_thb": "no",
        "notes": None,
        "status": "open",
        "external_id": "",
    }
    for field, bad in checks.items():
        item = dict(base)
        item[field] = bad
        status, err = call("POST", "/api/import", "10.21.0.15", {"items": [item]}, None)
        assert status == 400, field
        assert err["error"] == field
        if field != "external_id":
            assert err["external_id"] == "pwa-acqua"
        assert item_count() == kept

    item_id = next(item["id"] for item in logic.export_all()["items"] if item["external_id"] == "pwa-acqua")
    status, _ = call("POST", f"/api/items/{item_id}/done", "8.8.8.8", {}, None)
    assert status == 401
    status, done = call("POST", f"/api/items/{item_id}/done", "10.21.0.15", {}, None)
    assert status == 200
    assert done["done"] is True
    status, _ = call("POST", f"/voce/{item_id}/fatto", "203.0.113.9", {}, None)
    assert status in (200, 303)

    import sqlite3

    conn = sqlite3.connect(logic.DB_PATH)
    conn.execute("DELETE FROM history")
    conn.execute("DELETE FROM items")
    conn.commit()
    conn.close()
    assert logic.export_all()["items"] == []
