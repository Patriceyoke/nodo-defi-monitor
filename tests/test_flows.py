"""Quantity drops become outflow events. Price moves do not. Addresses here are fake."""
import time

import pytest

from app import config, db
from app.flows import meaningful_quantity_drop, observe_positions, public_flow


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "monitor.db")
    monkeypatch.setattr(config, "_READY", True)
    monkeypatch.setattr(config, "DATA_PERSISTENT", True)
    db.init_db()
    db.set_setting("outflow_drop_pct", "2")
    db.set_setting("outflow_usd", "500")


def _position(amount, usd, asset="USDC", protocol="Aave"):
    return {
        "owner": "io",
        "protocol": protocol,
        "asset": asset,
        "chain": "ethereum",
        "address": "0x" + "11" * 20,
        "amount": amount,
        "usd": usd,
    }


def test_price_move_is_not_an_outflow():
    assert meaningful_quantity_drop(10000, 10000, 10000, 2, 500) is None


def test_withdrawal_of_token_quantity_is_an_outflow(isolated):
    observe_positions([_position(10000, 10000)], now=1)
    assert db.list_fund_flows() == []
    observe_positions([_position(7500, 7500)], now=2)
    rows = db.list_fund_flows()
    assert len(rows) == 1
    assert rows[0]["direction"] == "out"
    assert rows[0]["delta"] == 2500
    assert rows[0]["protocol"] == "Aave"
    assert rows[0]["asset"] == "USDC"
    assert rows[0]["status"] == "open"
    shown = public_flow(rows[0])
    assert shown["urgent"] is True
    assert shown["confirmation"] == "open"
    assert "wallet" not in shown
    observe_positions([_position(7500, 7000)], now=3)
    assert len(db.list_fund_flows()) == 1


def test_small_quantity_change_stays_quiet_and_inflows_are_not_urgent(isolated):
    observe_positions([_position(100, 100)], now=1)
    observe_positions([_position(99.5, 99.5)], now=2)
    assert db.list_fund_flows() == []
    observe_positions([_position(150, 150)], now=3)
    rows = db.list_fund_flows()
    assert rows[0]["direction"] == "in"
    assert public_flow(rows[0])["urgent"] is False


def test_answer_is_kept_when_the_same_balance_is_read_again(isolated):
    observe_positions([_position(10000, 10000)], now=1)
    observe_positions([_position(7500, 7500)], now=2)
    flow_id = db.list_fund_flows()[0]["id"]
    assert db.set_fund_flow_status(flow_id, "mine") is True
    observe_positions([_position(7500, 7500)], now=int(time.time()))
    row = db.list_fund_flows()[0]
    assert row["status"] == "mine"
    assert public_flow(row)["urgent"] is False
    assert db.set_fund_flow_status(flow_id, "suspicious") is True
    assert public_flow(db.list_fund_flows()[0])["urgent"] is True
