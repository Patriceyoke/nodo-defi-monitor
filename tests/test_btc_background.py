"""Bitcoin scans must not hold the dashboard refresh."""
from __future__ import annotations

import asyncio
import json
import threading
import time

import pytest
from embit.descriptor.checksum import add_checksum
from fastapi.testclient import TestClient

from app import bitcoin, config, db, ens, evm, markets, portfolio
from app.bitcoin import _scripthash, derive_addresses
from app.main import app
from app.protocols import WETH

XPUB = "xpub661MyMwAqRbcFhCvdhTAfpEEDV58oqDvv65YNHC686NNs4KbH8YZQJWVmrfbve7aAVHzxw8bKFxA7MLeDK6BbLfkE3bqkvHLPgaGHHtYGeY"


def _descriptor() -> str:
    key = f"[ab12cd34/48h/0h/0h/2h]{XPUB}/<0;1>/*"
    return add_checksum(f"wsh(sortedmulti(2,{key},{key},{key}))")


class _Server:
    def __init__(self, target: str):
        self.target = target
        self.listunspent = 0
        self.server = None

    def start_background(self) -> int:
        self.loop = asyncio.new_event_loop()
        ready = threading.Event()
        holder: list[int] = []

        def _run() -> None:
            asyncio.set_event_loop(self.loop)
            self.server = self.loop.run_until_complete(asyncio.start_server(self._handle, "127.0.0.1", 0))
            holder.append(self.server.sockets[0].getsockname()[1])
            ready.set()
            self.loop.run_forever()

        self.thread = threading.Thread(target=_run, daemon=True)
        self.thread.start()
        ready.wait(5)
        return holder[0]

    def stop_background(self) -> None:
        if not getattr(self, "loop", None):
            return
        self.loop.call_soon_threadsafe(self.server.close)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(3)

    def _result(self, method: str, params: list) -> object:
        if method == "server.version":
            return ["Electrs 0.11.1", "1.4"]
        if method == "server.features":
            return {"genesis_hash": "00"}
        if method == "blockchain.headers.subscribe":
            return {"height": 800000, "hex": "00"}
        if method == "blockchain.scripthash.subscribe":
            return "ab" * 32
        if method == "blockchain.scripthash.get_history":
            if params and params[0] == self.target:
                return [{"tx_hash": "cd" * 32, "height": 10}]
            return []
        if method == "blockchain.scripthash.listunspent":
            self.listunspent += 1
            if params and params[0] == self.target:
                return [{"tx_hash": "cd" * 32, "tx_pos": 0, "height": 10, "value": 20_000_000}]
            return []
        if method == "blockchain.scripthash.get_balance":
            return {"confirmed": 0, "unconfirmed": 0}
        if method == "blockchain.transaction.get":
            return {"vin": [], "vout": []}
        return None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        buf = b""
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if not line.strip():
                        continue
                    msg = json.loads(line)
                    body = {"id": msg.get("id"), "jsonrpc": "2.0", "result": self._result(msg.get("method"), msg.get("params") or [])}
                    writer.write((json.dumps(body) + "\n").encode())
                    await writer.drain()
        finally:
            writer.close()


def _install_mocks(monkeypatch):
    async def fake_rpc(method, params, url=None, chain="ethereum"):
        if method == "eth_blockNumber":
            evm._remember_rpc(chain, "http://mock-rpc", True, None)
            return "0x10"
        if method == "eth_getBalance":
            return hex(10**18)
        if method == "eth_gasPrice":
            return "0x1"
        return "0x0"

    async def fake_prices(tokens):
        return {"ETH": 2500.0, f"ethereum:{WETH.lower()}": 2500.0}

    monkeypatch.setattr(evm, "eth_rpc", fake_rpc)
    monkeypatch.setattr(markets, "prices_usd", fake_prices)
    monkeypatch.setattr(markets, "btc_price_usd", lambda: _async_value(50_000.0))
    monkeypatch.setattr(markets, "fx_thb", lambda: _async_value(35.0))
    monkeypatch.setattr(markets, "llama_pools", lambda: _async_value([]))
    monkeypatch.setattr(markets, "protocol_health", lambda: _async_value([]))
    monkeypatch.setattr(ens, "scan_wallets", lambda wallets: _async_value([]))


async def _async_value(value):
    return value


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "monitor.db")
    monkeypatch.setattr(config, "_READY", True)
    monkeypatch.setattr(config, "DATA_PERSISTENT", True)
    portfolio.invalidate()
    bitcoin._forget_working()
    bitcoin._PROGRESS.clear()
    bitcoin._SCAN_TASK = None
    bitcoin._SCRIPT_CACHE.clear()
    db.init_db()
    yield
    portfolio.invalidate()
    bitcoin._SCAN_TASK = None


def test_refresh_returns_before_the_multisig_scan_finishes(isolated_db, monkeypatch):
    _install_mocks(monkeypatch)
    desc = _descriptor()
    target = _scripthash(derive_addresses(desc, gap=1)[0]["scriptpubkey"])
    server = _Server(target)
    monkeypatch.setattr(bitcoin, "_gateway_hosts", lambda: [])
    monkeypatch.delenv("GATEWAY_IP", raising=False)
    monkeypatch.delenv("DEVICE_DOMAIN_NAME", raising=False)
    port = server.start_background()
    try:
        db.set_setting("electrs_host", "127.0.0.1")
        db.set_setting("electrs_port", str(port))
        db.set_setting("btc_gap_limit", "2")
        db.add_btc("Bitkey", "multisig", desc, "io")
        with TestClient(app) as client:
            started = time.perf_counter()
            response = client.post("/api/refresh?owner=tutti")
            elapsed = time.perf_counter() - started
            assert response.status_code == 200
            assert elapsed < 3
            body = response.json()
            assert body["btc_progress"]["running"] is True
            wallets = body["btc_wallets"]
            assert wallets and wallets[0]["label"] == "Bitkey"
            deadline = time.time() + 20
            progress = body["btc_progress"]
            while progress["running"] and time.time() < deadline:
                progress = client.get("/api/btc/progress").json()
            assert progress["running"] is False
            fresh = client.get("/api/portfolio?owner=tutti").json()
            wallet = fresh["btc_wallets"][0]
            assert wallet["btc"] == pytest.approx(0.2)
            assert wallet["scan"]["utxo_count"] == 1
            assert wallet["scan"]["confirmed_sats"] == 20_000_000
            assert wallet["stale"] is False
            first_reads = server.listunspent
            assert first_reads > 0
            again = client.post("/api/refresh?owner=tutti")
            assert again.status_code == 200
            assert again.elapsed.total_seconds() < 3
            deadline = time.time() + 20
            progress = again.json()["btc_progress"]
            while progress["running"] and time.time() < deadline:
                progress = client.get("/api/btc/progress").json()
            second = client.get("/api/portfolio?owner=tutti").json()["btc_wallets"][0]
            assert second["btc"] == pytest.approx(0.2)
            assert server.listunspent == first_reads
    finally:
        server.stop_background()
