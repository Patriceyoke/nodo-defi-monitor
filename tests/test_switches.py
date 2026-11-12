
from urllib.parse import urlparse

from app.protocols import USDC
from app.switches import (
    OFFICIAL_HOSTS,
    estimate_gas_usd,
    mark_switch_hides,
    official_yield_url,
    suggest_switches,
    switch_is_hidden,
)


def _pos(owner, protocol, asset, usd, apy, risk=None):
    return {
        "owner": owner,
        "protocol": protocol,
        "asset": asset,
        "usd": usd,
        "apy": apy,
        "risk": risk,
        "alternatives": [],
    }


def test_suggests_higher_yield_same_risk_after_gas():
    rows = [
        _pos("io", "Aave", "USDT", 15_000, 3.6),
        _pos("io", "Compound", "USDT", 1_000, 5.2),
    ]
    ideas = suggest_switches(rows, gas_usd=10)
    assert ideas
    idea = ideas[0]
    assert idea["from"] == "Aave USDT"
    assert idea["to"] == "Compound USDT"
    assert idea["net_year_usd"] == round(15000 * (5.2 - 3.6) / 100 - 10, 2)
    assert idea["owner"] == "io"


def test_rejects_higher_risk():
    rows = [
        _pos("io", "Aave", "USDC", 10_000, 4.0),
        _pos("io", "Fluid", "USDC", 1, 8.0),
    ]
    assert suggest_switches(rows, gas_usd=1) == []


def test_rejects_when_gas_eats_the_gain():
    rows = [
        _pos("io", "Aave", "USDT", 100, 3.6),
        _pos("io", "Compound", "USDT", 1, 5.2),
    ]
    # gross = 100 * 1.6% = 1.6, gas 10 -> rejected
    assert suggest_switches(rows, gas_usd=10) == []


def test_does_not_cross_owner_or_bucket():
    rows = [
        _pos("io", "Aave", "USDT", 15_000, 3.0),
        _pos("moglie", "Compound", "USDT", 1, 6.0),
        _pos("io", "Lido", "stETH", 1, 8.0),
    ]
    assert suggest_switches(rows, gas_usd=1) == []


def test_hidden_switch_returns_only_when_the_gap_grows_or_the_target_changes():
    idea = {"owner": "io", "from": "Aave USDT", "to": "Compound USDT", "from_apy": 3.6, "to_apy": 5.2}
    hidden_at = 1_700_000_000
    assert switch_is_hidden(idea, 1.6, hidden_at, hidden_at + 6 * 86400) is True
    assert switch_is_hidden(idea, None) is False
    # 1.6 + 0.9 stays hidden; 1.6 + 1.0 comes back.
    assert switch_is_hidden({**idea, "to_apy": 6.1}, 1.6, hidden_at, hidden_at + 86400) is True
    assert switch_is_hidden({**idea, "to_apy": 6.2}, 1.6, hidden_at, hidden_at + 86400) is False
    # Still valid after 7 days: show it again.
    assert switch_is_hidden(idea, 1.6, hidden_at, hidden_at + 7 * 86400) is False
    marked, release = mark_switch_hides(
        [idea, {**idea, "to": "Spark USDT", "to_apy": 5.2}, {**idea, "owner": "moglie"}],
        [{"owner": "io", "from_position": "Aave USDT", "to_target": "Compound USDT", "spread": 1.6, "created_at": hidden_at}],
        now=hidden_at + 2 * 86400,
    )
    by_key = {(row["owner"], row["to"]): row["hidden"] for row in marked}
    assert by_key[("io", "Compound USDT")] is True
    assert by_key[("io", "Spark USDT")] is False
    assert by_key[("moglie", "Compound USDT")] is False
    assert release == []
    _marked, due = mark_switch_hides(
        [idea],
        [{"owner": "io", "from_position": "Aave USDT", "to_target": "Compound USDT", "spread": 1.6, "created_at": hidden_at}],
        now=hidden_at + 7 * 86400,
    )
    assert due == [("io", "Aave USDT", "Compound USDT")]
    assert _marked[0]["hidden"] is False


def test_switch_hide_survives_restart(tmp_path, monkeypatch):
    from app import config, db

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "monitor.db")
    monkeypatch.setattr(config, "_READY", True)
    monkeypatch.setattr(config, "DATA_PERSISTENT", True)
    db.init_db()
    db.add_evm("Casa", "0x" + "ab" * 20, "io")
    db.hide_switch("io", "Aave USDT", "Compound USDT", 1.6)
    db.init_db()
    assert db.list_evm()[0]["label"] == "Casa"
    assert db.list_switch_hides()[0]["spread"] == 1.6


def test_yield_links_stay_on_official_apps():
    aave = official_yield_url("Aave", "USDC")
    assert "underlyingAsset=" + USDC in aave
    assert "marketName=proto_mainnet_v3" in aave
    assert official_yield_url("Compound", "WETH") == "https://app.compound.xyz/markets/weth-mainnet"
    assert official_yield_url("Spark", "USDT") == "https://app.spark.finance/savings/spusdt"
    assert "proto_spark_v3" in official_yield_url("SparkLend", "USDC")
    assert official_yield_url("Lido", "wstETH") == "https://stake.lido.fi/wrap"
    assert official_yield_url("Sky", "stUSDS") == "https://app.sky.money/stake"
    assert official_yield_url("Fluid", "USDC") == "https://fluid.io/1/lending"
    assert official_yield_url("Not a protocol", "USDC") == ""
    for protocol, asset in (
        ("Aave", "DAI"), ("Compound", "ETH"), ("Spark", "USDC"), ("SparkLend", "WETH"),
        ("Fluid BNB", "USDT"), ("Lido", "stETH"), ("Sky", "sUSDS"),
        ("Rocket Pool", "rETH"), ("Maple", "USDC"), ("Aave", "UNKNOWN"),
    ):
        url = official_yield_url(protocol, asset)
        assert url.startswith("https://")
        assert urlparse(url).hostname in OFFICIAL_HOSTS
    ideas = suggest_switches([
        _pos("io", "Aave", "USDT", 15_000, 3.6),
        _pos("io", "Compound", "USDT", 1_000, 5.2),
    ], gas_usd=10)
    assert ideas[0]["url"] == "https://app.compound.xyz/markets/usdt-mainnet"


def test_gas_estimate():
    # 20 gwei * 220000 / 1e18 * 2000$ ≈ 8.8
    wei = 20 * 10**9
    usd = estimate_gas_usd(wei, 2000)
    assert 8 < usd < 10
    assert estimate_gas_usd(wei, 2000, override=3) == 3
