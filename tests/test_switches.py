
from app.switches import estimate_gas_usd, suggest_switches


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


def test_gas_estimate():
    # 20 gwei * 220000 / 1e18 * 2000$ ≈ 8.8
    wei = 20 * 10**9
    usd = estimate_gas_usd(wei, 2000)
    assert 8 < usd < 10
    assert estimate_gas_usd(wei, 2000, override=3) == 3
