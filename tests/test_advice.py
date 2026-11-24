"""One portfolio-wide recommendation. Addresses below are the usual fakes."""
import json

from app.portfolio import failed_view
from app.switches import (
    ADVICE_MESSAGE,
    ADVICE_REASON_CAPS,
    ADVICE_REASON_ELSE,
    ADVICE_REASON_GAS,
    best_advice,
)

FAKE = "0x" + "11" * 20


def _usdc(owner, label, protocol, usd, apy, alternatives=None):
    return {
        "owner": owner,
        "label": label,
        "protocol": protocol,
        "asset": "USDC",
        "usd": usd,
        "apy": apy,
        "apy30": apy,
        "address": FAKE,
        "alternatives": alternatives or [],
    }


def test_protocol_cap_limits_the_move_to_the_remaining_room():
    compound = _usdc("io", "Altro", "Compound", 20_000, 8.0)
    source = _usdc("io", "Esempio", "Aave", 80_000, 3.0, alternatives=[
        {"protocol": "Sky", "asset": "sUSDS", "apy": 20.0, "apy30": 20.0, "owner": None, "tvl_usd": 200_000_000},
    ])
    result = best_advice([source, compound], gas_usd=1, protocol_cap_pct=25, issuer_cap_pct=40)
    card = result["advice"]
    assert isinstance(card, dict)
    assert card["to"] == "Compound USDC"
    assert card["usd"] == 5000
    assert card["full_position_usd"] == 80_000
    assert card["amount_limited"] is True
    assert card["wallet_label"] == "Esempio"
    assert card["owner"] == "io"
    assert "address" not in card
    assert "wallet_key" not in card
    assert "0x" not in json.dumps(card)


def test_single_recommendation_is_a_dict():
    source = _usdc("io", "Esempio", "Aave", 20_000, 3.0, alternatives=[
        {"protocol": "Compound", "asset": "USDC", "apy": 5.0, "apy30": 5.0, "owner": None, "tvl_usd": 200_000_000, "utilization": 40},
        {"protocol": "Lido", "asset": "stETH", "apy": 9.0, "apy30": 4.0, "owner": None, "tvl_usd": 200_000_000},
    ])
    result = best_advice([source], gas_usd=1)
    assert isinstance(result["advice"], dict)
    assert not isinstance(result["advice"], list)
    assert result["advice"]["to_protocol"] == "Compound"


def test_no_recommendation_uses_the_exact_message_and_a_reason():
    quiet = best_advice([_usdc("io", "Esempio", "Aave", 1_000, 4.0)], gas_usd=1)
    assert quiet["advice"] is None
    assert quiet["message"] == ADVICE_MESSAGE
    assert quiet["reason"] == ADVICE_REASON_ELSE

    gas = best_advice([
        _usdc("io", "Esempio", "Aave", 200, 4.0, alternatives=[
            {"protocol": "Compound", "asset": "USDC", "apy": 4.2, "apy30": 4.2, "owner": None, "tvl_usd": 200_000_000},
        ]),
    ], gas_usd=10)
    assert gas["advice"] is None
    assert gas["message"] == ADVICE_MESSAGE
    assert gas["reason"] == ADVICE_REASON_GAS

    capped = best_advice([
        _usdc("io", "Esempio", "Aave", 70_000, 3.0),
        _usdc("io", "Altro", "Compound", 30_000, 8.0),
    ], gas_usd=1, protocol_cap_pct=25, issuer_cap_pct=40)
    assert capped["advice"] is None
    assert capped["message"] == ADVICE_MESSAGE
    assert capped["reason"] == ADVICE_REASON_CAPS


def test_advice_names_the_wallet_and_hides_the_address():
    source = _usdc("moglie", "Esempio", "Aave", 10_000, 3.0, alternatives=[
        {"protocol": "Compound", "asset": "USDC", "apy": 6.0, "apy30": 6.0, "owner": None, "tvl_usd": 200_000_000},
    ])
    card = best_advice([source], gas_usd=1)["advice"]
    assert card["wallet_label"] == "Esempio"
    assert card["owner"] == "moglie"
    assert "address" not in card
    assert FAKE not in json.dumps(card)


def test_failed_view_carries_empty_advice():
    view = failed_view("tutti", ["rete"])
    assert view["advice"]["advice"] is None
    assert view["advice"]["message"] == ADVICE_MESSAGE
    assert view["advice"]["rejected"] == []
