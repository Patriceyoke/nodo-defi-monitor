"""End-to-end security cache: a 0.6.2 blob is reclassified, and refresh does not block."""
from __future__ import annotations

import asyncio
import json
import threading
import time

from fastapi.testclient import TestClient

WALLET = "0x" + "11" * 20
OLD_SCANNED = 1_700_000_000


def _isolate(monkeypatch, tmp_path):
    from app import config, db

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "monitor.db")
    monkeypatch.setattr(config, "_READY", True)
    monkeypatch.setattr(config, "DATA_PERSISTENT", True)
    db.init_db()
    return db


def _old_blob() -> dict:
    return {
        "scanned_at": OLD_SCANNED,
        "cached_at": OLD_SCANNED,
        "partial": True,
        "note": "vecchia finestra",
        "readonly": True,
        "approvals": [],
        "incidents": [
            {
                "protocol": "Fluid",
                "name": "Fluidkey / Database Breach",
                "owners": ["io"],
                "severity": "danger",
                "detail": "exploit",
            },
            {
                "protocol": "Lido",
                "name": "Lido Earn: Introducing Two Layers of Loss Protection",
                "owners": ["io"],
                "severity": "danger",
                "detail": "comunicazione ufficiale",
            },
            {
                "protocol": "Lido",
                "name": "Lido V3 & Myrmidon Staking",
                "owners": ["io"],
                "severity": "danger",
                "detail": "comunicazione ufficiale",
            },
            {
                "protocol": "Aave",
                "name": "Aave exploit",
                "owners": ["io"],
                "severity": "danger",
                "detail": "exploit",
            },
        ],
        "outflows": [],
        "alerts": [
            {
                "owner": "io",
                "level": "danger",
                "kind": "incident",
                "title": "Protocollo Fluid",
                "text": "Fluidkey / Database Breach: exploit.",
            },
            {
                "owner": "io",
                "level": "danger",
                "kind": "incident",
                "title": "Protocollo Lido",
                "text": "Lido Earn: Introducing Two Layers of Loss Protection: comunicazione ufficiale.",
            },
            {
                "owner": "io",
                "level": "danger",
                "kind": "incident",
                "title": "Protocollo Lido",
                "text": "Lido V3 & Myrmidon Staking: comunicazione ufficiale.",
            },
            {"owner": "io", "level": "danger", "kind": "outflow", "title": "ETH", "text": "uscita"},
        ],
        "errors": [],
    }


def _blob_text(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False)


def test_api_reclassifies_a_cached_062_report(tmp_path, monkeypatch):
    from app.main import app
    from app import security

    db = _isolate(monkeypatch, tmp_path)
    db.add_evm("Casa", WALLET, "io")
    db.cache_set("security", _old_blob())
    security.migrate_security_cache()

    with TestClient(app) as client:
        response = client.get("/api/security")
    assert response.status_code == 200
    body = response.json()
    text = _blob_text(body)
    assert body["running"] is False
    assert body["from_cache"] is True
    assert body["partial"] is False
    assert body["scanned_at"] == OLD_SCANNED
    assert "Fluidkey" not in text
    assert "Protocollo Fluid" not in text
    assert "comunicazione ufficiale" not in text
    assert "Introducing Two Layers" not in text
    assert "Myrmidon" not in text
    assert "Ritira" not in text
    names = [row["name"] for row in body["incidents"]]
    assert names == ["Aave exploit"]
    titles = [row["title"] for row in body["alerts"]]
    assert "Protocollo Fluid" not in titles
    assert "Protocollo Aave" in titles
    assert "ETH" in titles
    urgent = " ".join(body["summary"]["urgent_keys"])
    assert "Fluid" not in urgent
    assert "Lido" not in urgent
    assert "aave" in urgent

    stored = db.cache_get("security")
    stored_text = _blob_text(stored)
    assert "Fluidkey" not in stored_text
    assert "comunicazione ufficiale" not in stored_text
    assert stored["partial"] is False
    assert db.list_evm()[0]["address"] == WALLET


def test_http_refresh_returns_immediately_and_a_later_get_is_fresh(tmp_path, monkeypatch):
    from app.main import app
    from app import security

    db = _isolate(monkeypatch, tmp_path)
    db.cache_set("security", _old_blob())
    release = threading.Event()

    async def scan():
        await asyncio.to_thread(release.wait, 5)
        db.cache_set("security", {
            "scanned_at": 99,
            "cached_at": 99,
            "from_cache": False,
            "partial": False,
            "note": security._scan_note(False, []),
            "approvals": [],
            "incidents": [],
            "outflows": [],
            "alerts": [],
            "errors": [],
        })

    monkeypatch.setattr(security, "scan_and_store", scan)
    try:
        with TestClient(app) as client:
            started = time.perf_counter()
            response = client.get("/api/security?refresh=true")
            elapsed = time.perf_counter() - started
            assert response.status_code == 200
            body = response.json()
            assert elapsed < 2
            assert body["running"] is True
            assert body["from_cache"] is True
            assert body["scanned_at"] == OLD_SCANNED
            assert "Fluidkey" not in _blob_text(body)
            release.set()
            fresh = None
            for _ in range(50):
                fresh = client.get("/api/security").json()
                if fresh["scanned_at"] == 99 and fresh["running"] is False:
                    break
                time.sleep(0.05)
        assert fresh["from_cache"] is False
        assert fresh["partial"] is False
        assert fresh["running"] is False
        assert fresh["scanned_at"] == 99
    finally:
        security.reset_scan_state()


def test_mcp_refresh_does_not_block_and_the_next_call_is_fresh(tmp_path, monkeypatch):
    from app.mcp_server import call_tool
    from app import security

    db = _isolate(monkeypatch, tmp_path)
    db.cache_set("security", _old_blob())
    release = asyncio.Event()

    async def scan():
        await release.wait()
        db.cache_set("security", {
            "scanned_at": 99,
            "cached_at": 99,
            "from_cache": False,
            "partial": False,
            "note": security._scan_note(False, []),
            "approvals": [],
            "incidents": [],
            "outflows": [],
            "alerts": [],
            "errors": [],
        })

    async def run():
        monkeypatch.setattr(security, "scan_and_store", scan)
        started = time.perf_counter()
        first = await call_tool("get_security", {"refresh": True, "owner": "tutti"})
        elapsed = time.perf_counter() - started
        payload = json.loads(first["content"][0]["text"])
        assert elapsed < 2
        assert payload["running"] is True
        assert payload["from_cache"] is True
        assert payload["scanned_at"] == OLD_SCANNED
        text = first["content"][0]["text"]
        assert "Fluidkey" not in text
        assert "Protocollo Fluid" not in text
        assert "comunicazione ufficiale" not in text
        release.set()
        await security._SCAN
        second = await call_tool("get_security", {"owner": "tutti"})
        done = json.loads(second["content"][0]["text"])
        assert done["running"] is False
        assert done["from_cache"] is False
        assert done["partial"] is False
        assert done["scanned_at"] == 99

    try:
        asyncio.run(run())
    finally:
        security.reset_scan_state()


def test_stuck_running_flag_clears_on_the_next_read(tmp_path, monkeypatch):
    from app.main import app
    from app import security

    db = _isolate(monkeypatch, tmp_path)
    db.cache_set("security", _old_blob())

    class _Hung:
        def done(self):
            return False

        def cancel(self):
            return True

    try:
        with TestClient(app) as client:
            security._SCAN = _Hung()
            security._SCAN_STARTED = time.time() - (security.SCAN_TIMEOUT + 30)
            body = client.get("/api/security").json()
        assert body["running"] is False
        assert body["partial"] is True
        assert "tempo scaduto" in body["note"]
        assert body["scanned_at"] == OLD_SCANNED
        assert "Fluidkey" not in _blob_text(body)
    finally:
        security.reset_scan_state()


def test_a_chain_that_times_out_is_partial_with_a_reason(tmp_path, monkeypatch):
    from app import security

    db = _isolate(monkeypatch, tmp_path)
    db.add_evm("Casa", WALLET, "io")
    db.cache_set("portfolio", {"positions": [{
        "address": WALLET, "chain": "ethereum", "protocol": "Aave", "asset": "USDC",
        "amount": 1, "usd": 10, "owner": "io",
    }]})
    monkeypatch.setattr(security, "CHAIN_TIMEOUT", 0.05)

    async def slow_logs(chain, address, chunk_blocks=400_000):
        await asyncio.sleep(1)
        return [], False

    async def empty(*_args, **_kwargs):
        return []

    monkeypatch.setattr(security, "_approval_logs", slow_logs)
    monkeypatch.setattr(security, "_hacks", empty)
    monkeypatch.setattr(security, "_official_notes", empty)
    monkeypatch.setattr(security, "_pause_flags", empty)

    report = asyncio.run(security.scan_and_store())
    assert report["partial"] is True
    assert report["from_cache"] is False
    assert any("tempo scaduto" in item for item in report["errors"])
    assert "tempo scaduto" in report["note"]
    assert all("polygon" not in item and "arbitrum" not in item and "base" not in item for item in report["errors"])
    assert db.list_evm()[0]["address"] == WALLET
