"""Electrs inside Umbrel: umbrel.local does not resolve, so try container names."""
from __future__ import annotations

import asyncio

from app import bitcoin, db
from app.btc_security import evaluate


def _settings(mapping):
    def get_setting(key, default=""):
        return mapping.get(key, default)
    return get_setting


def _install(monkeypatch, settings, request, gateways=None, resolves=None):
    bitcoin._forget_working()
    monkeypatch.setattr(db, "get_setting", _settings(settings))
    monkeypatch.setattr(bitcoin, "_electrum_request", request)
    monkeypatch.setattr(bitcoin, "_gateway_hosts", lambda: list(gateways or []))
    if resolves is not None:
        monkeypatch.setattr(bitcoin, "_host_resolves", resolves)


def test_saved_umbrel_local_uses_the_electrs_container(monkeypatch):
    calls = []
    saved = {"electrs_host": "umbrel.local", "electrs_port": "50001"}

    def request(host, port, method, params, timeout=8.0, plain=False):
        calls.append((host, port, plain, method))
        if method == "server.version" and (host, port) == ("electrs_electrs_1", 50001):
            return ["Electrs 0.10"]
        if method == "server.version":
            raise OSError("[Errno -2] Name or service not known")
        if method == "blockchain.headers.subscribe":
            return {"height": 800000}
        if method == "blockchain.scripthash.get_balance":
            return {"confirmed": 0, "unconfirmed": 0}
        raise OSError("no")

    _install(monkeypatch, saved, request, gateways=["172.17.0.1"], resolves=lambda host: True)
    status = asyncio.run(bitcoin.electrs_status())
    assert status["connected"] is True
    assert "Collegato via electrs_electrs_1:50001" in status["detail"]
    versions = [item for item in calls if item[3] == "server.version"]
    assert versions[0][:2] == ("umbrel.local", 50001)
    assert versions[1][:3] == ("electrs_electrs_1", 50001, True)
    assert all(item[0] != "172.17.0.1" for item in calls)
    assert saved["electrs_host"] == "umbrel.local"


def test_fulcrum_plain_tcp_comes_after_electrs_names(monkeypatch):
    calls = []

    def request(host, port, method, params, timeout=8.0, plain=False):
        calls.append((host, port, plain, method))
        if method != "server.version":
            if method == "blockchain.headers.subscribe":
                return {"height": 10}
            return {"confirmed": 0, "unconfirmed": 0}
        if (host, port) == ("fulcrum_fulcrum_1", 50001):
            return ["Fulcrum"]
        raise ConnectionRefusedError(host)

    _install(
        monkeypatch,
        {"electrs_host": "umbrel.local", "electrs_port": "50001"},
        request,
        resolves=lambda host: host != "umbrel.local",
    )
    status = asyncio.run(bitcoin.electrs_status())
    assert "Collegato via fulcrum_fulcrum_1:50001" in status["detail"]
    tried = [(host, port, plain) for host, port, plain, method in calls if method == "server.version"]
    assert tried[:4] == [
        ("electrs_electrs_1", 50001, True),
        ("electrs_app_1", 50001, True),
        ("fulcrum_fulcrum_1", 50002, True),
        ("fulcrum_fulcrum_1", 50001, True),
    ]
    assert tried[2][2] is True


def test_configured_host_wins_and_is_cached(monkeypatch):
    calls = []

    def request(host, port, method, params, timeout=8.0, plain=False):
        calls.append((host, method))
        if method == "blockchain.headers.subscribe":
            return {"height": 3}
        if method == "blockchain.scripthash.get_balance":
            return {"confirmed": 0, "unconfirmed": 0}
        if host == "electrs.internal":
            return ["ok"]
        raise OSError("no")

    _install(
        monkeypatch,
        {"electrs_host": "electrs.internal", "electrs_port": "50001"},
        request,
        resolves=lambda host: host == "electrs.internal",
    )
    first = asyncio.run(bitcoin.electrs_status())
    second = asyncio.run(bitcoin.electrs_status())
    assert "Collegato via electrs.internal:50001" in first["detail"]
    assert second["connected"] is True
    assert all(host == "electrs.internal" for host, _method in calls)


def test_local_name_hint_and_security_card():
    assert "electrs_electrs_1" in bitcoin.local_host_hint("umbrel.local")
    assert "electrs_electrs_1" in bitcoin.local_host_hint("bitcoin.local")
    assert bitcoin.local_host_hint("electrs_electrs_1") == ""
    checks = evaluate(
        [],
        {
            "configured": True,
            "connected": True,
            "host": "electrs_electrs_1",
            "port": 50001,
            "height": 5,
            "detail": "Collegato via electrs_electrs_1:50001. Altezza blocco 5.",
        },
    )
    assert checks[0]["title"] == "Nodo / Electrs"
    assert "Collegato via electrs_electrs_1:50001" in checks[0]["detail"]
