"""0.7.4: a failed vault read is not an outflow, and alerts name the wallet."""
import pytest

from app import config, db
from app.flows import list_public_flows, observe_positions

ADDR = "0x" + "11" * 20


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "monitor.db")
    monkeypatch.setattr(config, "_READY", True)
    monkeypatch.setattr(config, "DATA_PERSISTENT", True)
    db.init_db()
    db.set_setting("outflow_drop_pct", "2")
    db.set_setting("outflow_usd", "500")
    db.add_evm("Esempio", ADDR, "io")


def _fluid(amount, usd, **extra):
    return {"owner": "io", "label": "Esempio", "protocol": "Fluid", "asset": "USDC", "chain": "ethereum",
            "address": ADDR, "amount": amount, "usd": usd, "valuation_ok": True, **extra}


def test_share_count_from_a_failed_conversion_is_not_an_outflow(isolated):
    observe_positions([_fluid(5000, 5000)], now=1)
    # convertToAssets failed: the row carries 4060 shares, no price.
    observe_positions([_fluid(4060, None, valuation_ok=False)], now=2)
    assert db.list_fund_flows() == []
    observe_positions([_fluid(5001, 5001)], now=3)
    assert db.list_fund_flows() == []


def test_rows_kept_from_the_previous_refresh_are_not_readings(isolated):
    observe_positions([_fluid(5000, 5000)], now=1)
    observe_positions([_fluid(1000, 1000, stale=True)], now=2)
    assert db.list_fund_flows() == []


def test_a_real_withdrawal_names_the_wallet_and_time(isolated):
    observe_positions([_fluid(5000, 5000)], now=1)
    observe_positions([_fluid(4000, 4000)], now=1_795_000_000)
    rows = list_public_flows()
    assert len(rows) == 1
    assert rows[0]["label"] == "Esempio"
    assert rows[0]["detected_at"] == 1_795_000_000
    assert "wallet" not in rows[0] and ADDR not in str(rows[0])


def test_an_old_misread_closes_itself_when_the_amount_is_back(isolated):
    observe_positions([_fluid(5000, 5000)], now=1)
    observe_positions([_fluid(4060, 4060)], now=2)  # as 0.7.2 stored it
    assert [r["status"] for r in list_public_flows()] == ["open"]
    observe_positions([_fluid(5000.5, 5000.5)], now=3)
    assert [r["status"] for r in list_public_flows()] == ["noted"]


def test_an_answered_alert_is_left_alone(isolated):
    observe_positions([_fluid(5000, 5000)], now=1)
    observe_positions([_fluid(4000, 4000)], now=2)
    flow = list_public_flows()[0]
    db.set_fund_flow_status(flow["id"], "suspicious")
    observe_positions([_fluid(5000, 5000)], now=3)
    assert list_public_flows()[0]["status"] == "suspicious"


def test_security_scan_skips_misreads_and_empty_snapshots():
    from app.security import outflow_rows

    marks = [{"wallet": ADDR, "chain": "ethereum", "token": "fusdc", "amount": 5000.0, "usd": 5000.0,
              "owner": "io", "symbol": "USDC"}]
    misread = [{**_fluid(4060, None, valuation_ok=False), "token": "fusdc"}]
    alerts, updates = outflow_rows(misread, marks, {ADDR}, 2, 500)
    assert alerts == [] and updates == []
    # The empty snapshot of a failed refresh: nothing was read, so nothing "went to zero".
    alerts, updates = outflow_rows([], marks, {ADDR}, 2, 500)
    assert alerts == [] and updates == []
    real = [{**_fluid(4000, 4000), "token": "fusdc"}]
    alerts, _updates = outflow_rows(real, marks, {ADDR}, 2, 500)
    assert alerts[0]["label"] == "Esempio" and alerts[0]["detected_at"] > 0
