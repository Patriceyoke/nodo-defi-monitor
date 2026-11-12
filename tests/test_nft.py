"""Spam filter, sale suggestion, and wallet persistence."""
from __future__ import annotations

import pytest

from app import config, db
from app.nft import apply_user_hides, finalize, is_liquidity_position, spam_reason, suggest_sale


def _real(**extra):
    item = {
        "name": "Pezzo",
        "collection": "Collezione",
        "description": "Un'opera.",
        "token_type": "ERC721",
        "verified": True,
        "source_spam": False,
        "floor_native": 1.0,
        "floor_usd": 2000,
        "recent_sale_native": 1.1,
        "recent_sales_count": 3,
        "native_symbol": "ETH",
    }
    item.update(extra)
    return item


def test_spam_filter_hides_flagged_claims_links_and_unverified_1155():
    assert spam_reason(_real(source_spam=True))
    assert spam_reason(_real(name="Visit and claim your reward"))
    assert spam_reason(_real(description="voucher at https://example.invalid/airdrop"))
    assert spam_reason(_real(token_type="ERC-1155", verified=False, floor_native=2, floor_usd=10))
    assert spam_reason(_real(verified=False, floor_native=None, floor_usd=None, recent_sale_native=None, recent_sales_count=0))
    assert spam_reason(_real()) is None
    assert spam_reason(_real(token_type="ERC1155", verified=True)) is None


def test_suggestion_is_slightly_above_floor_or_says_unavailable():
    priced = suggest_sale(_real())
    assert priced["suggestion_native"] == pytest.approx(1.05)
    assert priced["suggestion_usd"] == pytest.approx(2100)
    assert "floor" in priced["suggestion_reason"].lower()
    assert priced["price_status"] == "ok"

    listed_low = suggest_sale(_real(list_price_native=0.4, listed=True))
    assert "sotto il floor" in listed_low["suggestion_reason"]

    no_floor = suggest_sale(_real(floor_native=None, floor_usd=None, recent_sale_native=0.8, recent_sale_usd=1600))
    assert no_floor["suggestion_native"] == pytest.approx(0.8)
    assert "24 ore" in no_floor["suggestion_reason"]

    empty = suggest_sale({"name": "x"})
    assert empty["suggestion_native"] is None
    assert empty["suggestion_usd"] is None
    assert empty["suggestion_reason"] == "Prezzo non disponibile."
    assert empty["price_status"] == "unavailable"


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "monitor.db")
    monkeypatch.setattr(config, "_READY", True)
    monkeypatch.setattr(config, "DATA_PERSISTENT", True)
    db.init_db()
    return db


def test_startup_never_wipes_wallets(isolated_db):
    assert "DROP TABLE" not in db.SCHEMA.upper()
    isolated_db.add_evm("tenuta", "0x000000000000000000000000000000000000dead", "io")
    isolated_db.set_setting("currency", "THB")
    isolated_db.set_setting("nft_api_key", "local-only")
    before = isolated_db.list_evm()
    isolated_db.init_db()
    isolated_db.init_db()
    after = isolated_db.list_evm()
    assert len(after) == len(before) == 1
    assert after[0]["address"] == before[0]["address"]
    assert after[0]["label"] == "tenuta"
    assert isolated_db.get_setting("currency") == "THB"
    assert isolated_db.get_setting("nft_api_key") == "local-only"


def test_import_adds_without_deleting_or_leaking_mcp_token(isolated_db):
    isolated_db.add_evm("gia", "0x000000000000000000000000000000000000dEaD", "moglie")
    token = isolated_db.get_setting("mcp_token")
    backup = isolated_db.export_backup()
    assert "mcp_token" not in backup["settings"]
    assert backup["evm"][0]["owner"] == "moglie"
    result = isolated_db.import_backup(
        {
            "evm": [
                {"label": "nuovo", "address": "0x000000000000000000000000000000000000bEEF", "owner": "io"},
                {"label": "gia", "address": "0x000000000000000000000000000000000000dead", "owner": "moglie"},
            ],
            "btc": [],
            "settings": {"currency": "USD", "mcp_token": "non-deve-entrare", "nft_api_provider": "alchemy"},
        }
    )
    assert result["evm_added"] == 1
    owners = {(w["address"].lower(), w["owner"]) for w in isolated_db.list_evm()}
    assert ("0x000000000000000000000000000000000000dead", "moglie") in owners
    assert ("0x000000000000000000000000000000000000beef", "io") in owners
    assert isolated_db.get_setting("mcp_token") == token
    assert isolated_db.get_setting("nft_api_provider") == "alchemy"
    assert isolated_db.get_setting("currency") == "USD"


def test_liquidity_positions_are_not_priced_as_collectibles():
    manager = "0xC36442b4a4522E871399CD717aBDD847Ab11FE88"
    assert is_liquidity_position({"contract": manager, "name": "1", "collection": "", "symbol": ""})
    assert is_liquidity_position({
        "contract": "0x" + "11" * 20,
        "name": "1",
        "collection": "Uniswap V3 Positions",
        "symbol": "UNI-V3-POS",
    })
    assert not is_liquidity_position({
        "contract": "0x" + "22" * 20,
        "name": "Blue square",
        "collection": "Paintings",
        "symbol": "ART",
    })
    done = finalize({
        "contract": manager,
        "name": "1",
        "collection": "Uniswap V3 Positions",
        "symbol": "UNI-V3-POS",
        "floor_native": 1,
        "floor_usd": 2000,
        "token_type": "ERC721",
        "verified": True,
        "source_spam": False,
    })
    assert done["liquidity"] is True
    assert done["hidden"] is False
    assert done["suggestion_usd"] is None
    assert done["suggestion_native"] is None
    assert done["floor_usd"] is None
    assert done["price_status"] == "lp"


def test_user_hide_is_saved_and_exported(isolated_db):
    item = {"chain": "ethereum", "contract": "0xabc", "token_id": "9", "hidden": False, "hidden_reason": ""}
    apply_user_hides(item, [{"kind": "token", "chain": "ethereum", "contract": "0xAbC", "token_id": "9"}])
    assert item["user_hidden"] is True
    assert item["hidden_reason"] == "Nascosto da te."
    contract = "0x" + "ab" * 20
    isolated_db.hide_nft("ethereum", contract, "9", "token")
    isolated_db.hide_nft("base", "0x" + "cd" * 20, "", "collection")
    isolated_db.init_db()
    assert len(isolated_db.list_nft_hides()) == 2
    backup = isolated_db.export_backup()
    assert {row["kind"] for row in backup["nft_hides"]} == {"token", "collection"}
    isolated_db.unhide_nft("ethereum", contract, "9", "token")
    assert len(isolated_db.list_nft_hides()) == 1
    isolated_db.import_backup({"evm": [], "btc": [], "settings": {}, "nft_hides": backup["nft_hides"]})
    assert len(isolated_db.list_nft_hides()) == 2
