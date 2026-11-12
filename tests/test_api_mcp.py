
from app import db
from app.mcp_server import handle_rpc, call_tool


def test_health(client):
    res = client.get("/api/health")
    assert res.status_code == 200
    assert res.json()["readonly"] is True


def test_reject_seed_on_evm_and_btc(client):
    seed = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"
    r = client.post("/api/wallets/evm", json={"label": "x", "address": seed, "owner": "io"})
    assert r.status_code == 400
    assert "seed" in r.json()["detail"].lower() or "chiave" in r.json()["detail"].lower()
    r = client.post("/api/wallets/btc", json={"label": "x", "descriptor": seed, "owner": "io"})
    assert r.status_code == 400


def test_add_public_eth_and_owner_filter(client):
    addr = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"
    r = client.post("/api/wallets/evm", json={"label": "Pubblico noto", "address": addr, "owner": "io"})
    assert r.status_code == 200, r.text
    r = client.post("/api/wallets/evm", json={"label": "Altro", "address": "0x000000000000000000000000000000000000dEaD", "owner": "moglie"})
    assert r.status_code == 200
    wallets = client.get("/api/wallets").json()
    assert len(wallets["evm"]) == 2
    owners = {w["owner"] for w in wallets["evm"]}
    assert owners == {"io", "moglie"}


def test_mcp_requires_token(client):
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    assert r.status_code == 401


def test_mcp_initialize_and_tools(client):
    token = db.get_setting("mcp_token")
    headers = {"Authorization": f"Bearer {token}"}
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}, headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert "protocolVersion" in body["result"]
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, headers=headers)
    names = {t["name"] for t in r.json()["result"]["tools"]}
    assert names == {
        "get_portfolio", "get_balances", "get_apys", "check_switch",
        "get_alerts", "get_history", "get_btc_wallets", "get_btc_security",
    }
    assert "sign" not in names and "send" not in names


import asyncio

def test_mcp_rejects_unknown_sign_tool():
    res = asyncio.run(call_tool("sign_transaction", {}))
    assert res["isError"] is True
