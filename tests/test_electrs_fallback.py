"""Electrs inside Umbrel: umbrel.local does not resolve, so try container names."""
from __future__ import annotations

import asyncio
import json
import socket

from app import bitcoin, db
from app.btc_security import evaluate


def _settings(mapping):
    def get_setting(key, default=""):
        return mapping.get(key, default)
    return get_setting


def _install(monkeypatch, settings, request, gateways=None, resolves=None):
    bitcoin._forget_working()
    bitcoin._GOOD = None
    bitcoin.db.cache_delete_prefix("electrs-good")
    if bitcoin._LIVE is not None:
        bitcoin._LIVE.close()
        bitcoin._LIVE = None
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
    assert versions[1][:2] == ("10.21.21.10", 50001)
    assert versions[2][:3] == ("electrs_electrs_1", 50001, True)
    assert all(item[0] != "172.17.0.1" for item in calls)
    assert saved["electrs_host"] == "umbrel.local"
    dns = [item for item in status["attempts"] if item["host"] == "umbrel.local"]
    assert dns and dns[0]["error"].startswith("Errore:")


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
    tried = [(host, port) for host, port, _plain, method in calls if method == "server.version"]
    assert tried.index(("10.21.21.10", 50001)) < tried.index(("electrs_electrs_1", 50001))
    assert tried.index(("10.21.21.200", 50002)) < tried.index(("fulcrum_fulcrum_1", 50001))
    dns = next(item for item in status["attempts"] if item["host"] == "umbrel.local")
    assert dns["error"] == "DNS: nome non risolto"
    refused = next(item for item in status["attempts"] if item["host"] == "10.21.21.10")
    assert refused["error"].startswith("Connessione rifiutata")
    assert status["network"]["gateway"] == "" or isinstance(status["network"]["gateway"], str)


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


def test_electrum_request_is_newline_json_and_retries_a_timeout(monkeypatch):
    sent = []

    def exchange(host, port, calls, timeout, plain):
        sent.append((calls, timeout, plain))
        frame = bitcoin._format_request(1, calls[0][0], calls[0][1])
        assert frame.endswith(b"\n")
        body = json.loads(frame.decode())
        assert body["params"] == ["nodo-defi-monitor", "1.4"]
        if len(sent) == 1:
            raise socket.timeout("timed out")
        return [["Electrs 0.11.1", "1.4"]]

    monkeypatch.setattr(bitcoin, "_electrum_exchange", exchange)
    monkeypatch.setattr(bitcoin, "_open_socket", lambda *args, **kwargs: (None, 4))
    result = bitcoin._electrum_request("10.21.21.10", 50001, "server.version", ["nodo-defi-monitor", "1.4"])
    assert result == ["Electrs 0.11.1", "1.4"]
    assert len(sent) == 2
    assert sent[0][1] == bitcoin.VERSION_TIMEOUT


def test_scripthash_batches_are_cached(monkeypatch):
    bitcoin._SCRIPT_CACHE.clear()
    calls = []

    def many(host, port, batch, timeout, plain):
        calls.append([item[1][0] for item in batch])
        return [{"confirmed": 1, "unconfirmed": 0} for _item in batch]

    monkeypatch.setattr(bitcoin, "_electrum_request_many", many)

    async def run():
        monkeypatch.setattr(bitcoin, "resolve_electrs", lambda: ("10.21.21.10", 50001, "10.21.21.10:50001", True))
        hashes = [f"{index:064x}" for index in range(25)]
        first = await bitcoin.query_scripthashes("blockchain.scripthash.get_balance", hashes)
        second = await bitcoin.query_scripthashes("blockchain.scripthash.get_balance", hashes)
        return first, second

    first, second = asyncio.run(run())
    assert len(calls) == 2
    assert len(calls[0]) == 20
    assert len(calls[1]) == 5
    assert first == second
    assert next(iter(second.values()))["confirmed"] == 1


def test_http_host_is_a_plain_address():
    assert bitcoin.normalize_electrum_host("http://192.168.1.137") == ("192.168.1.137", 50001)
    assert bitcoin.normalize_electrum_host("http://192.168.1.137:50001/") == ("192.168.1.137", 50001)
    assert bitcoin.normalize_electrum_host("ssl://electrs.local:50002/rpc") == ("electrs.local", 50002)
    assert bitcoin.normalize_electrum_host("tcp://10.21.21.10") == ("10.21.21.10", 50001)


def test_known_good_host_is_tried_first(monkeypatch):
    bitcoin._GOOD = ("10.21.21.10", 50001, True)
    monkeypatch.setattr(bitcoin.db, "get_setting", lambda key, default="": "http://192.168.1.137" if key == "electrs_host" else "50001")
    monkeypatch.setattr(bitcoin, "_gateway_hosts", lambda: [])
    hosts = [item[0] for item in bitcoin._candidate_targets()]
    assert hosts[0] == "10.21.21.10"
    assert "192.168.1.137" in hosts
    bitcoin._GOOD = None


def test_successful_steps_mark_the_card_connected():
    from app.btc_security import evaluate

    checks = evaluate([], {
        "configured": True,
        "connected": False,
        "host": "10.21.21.10",
        "port": 50001,
        "detail": "Electrs non ricontrollato:",
        "steps": [
            {"name": "TCP connect", "ok": True, "ms": 12, "detail": "10.21.21.10:50001"},
            {"name": "server.version", "ok": True, "ms": 40, "detail": "electrs/0.11.1 / 1.4"},
            {"name": "blockchain.headers.subscribe", "ok": True, "ms": 30, "detail": "altezza 975571"},
            {"name": "scripthash.get_balance", "ok": True, "ms": 20, "detail": "ok"},
        ],
    })
    assert checks[0]["label"] == "Connesso"
    assert checks[0]["tone"] == "success"
    assert "10.21.21.10:50001" in checks[0]["detail"]
    assert "975571" in checks[0]["detail"]
    assert "non ricontrollato" not in checks[0]["detail"]


def test_diagnostics_reuse_one_socket(monkeypatch):
    import json as _json

    class _Sock:
        def __init__(self):
            self.pending = b""

        def sendall(self, data):
            for line in data.split(b"\n"):
                if not line.strip():
                    continue
                msg = _json.loads(line)
                self.pending += (_json.dumps({"id": msg["id"], "result": ["Electrs 0.11.1", "1.4"]}) + "\n").encode()

        def recv(self, _size):
            if not self.pending:
                return b""
            chunk, self.pending = self.pending, b""
            return chunk

        def settimeout(self, _timeout):
            return None

        def close(self):
            return None

    opened = []

    def _open(host, port, plain):
        sock = _Sock()
        opened.append((host, port, plain, sock))
        return sock, 7

    bitcoin._GOOD = None
    if bitcoin._LIVE is not None:
        bitcoin._LIVE.close()
        bitcoin._LIVE = None
    monkeypatch.setattr(bitcoin, "_open_socket", _open)
    first = bitcoin._electrum_request("10.21.21.10", 50001, "server.version", ["nodo-defi-monitor", "1.4"], timeout=1, plain=True)
    second = bitcoin._electrum_request("10.21.21.10", 50001, "server.features", [], timeout=1, plain=True)
    assert first[0].startswith("Electrs")
    assert second[0].startswith("Electrs")
    assert len(opened) == 1
    bitcoin._LIVE.close()
    bitcoin._LIVE = None


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
