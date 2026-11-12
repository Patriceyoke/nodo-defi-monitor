"""Watch-only Bitcoin descriptors: Bitkey multisig, checksums, no private keys."""
from __future__ import annotations

import asyncio
import socket

import pytest

from app.bitcoin import derive_addresses, electrs_status, mask_key
from app.portfolio import income_figures
from app.validation import RejectedSecretError, classify_btc_key
from embit.descriptor.checksum import add_checksum

XPUB = "xpub661MyMwAqRbcFhCvdhTAfpEEDV58oqDvv65YNHC686NNs4KbH8YZQJWVmrfbve7aAVHzxw8bKFxA7MLeDK6BbLfkE3bqkvHLPgaGHHtYGeY"
ORIGIN = "[ab12cd34/48h/0h/0h/2h]"
XPRV = "xprv9s21ZrQH143K3QTDL4LXw2F7HEK3wJUD2nW2nRk4stbPy6cq3jPPqjiChkVvvNKmPGJxWUtg6LnF5kejMRNNU3TGtRBeJgk33yuGBxrMPHi"


def _wsh(path: str) -> str:
    key = f"{ORIGIN}{XPUB}/{path}"
    return add_checksum(f"wsh(sortedmulti(2,{key},{key},{key}))")


def test_bitkey_lines_match_multipath():
    external = _wsh("0/*")
    internal = _wsh("1/*")
    combined = _wsh("<0;1>/*")
    separate = derive_addresses(external + "\n" + internal, gap=2)
    together = derive_addresses(combined, gap=2)
    assert [item["address"] for item in separate] == [item["address"] for item in together]
    assert {item["role"] for item in separate} == {"receive", "change"}
    assert all(item["address"].startswith("bc1q") for item in separate)
    apostrophe = add_checksum(
        f"wsh(sortedmulti(2,[ab12cd34/48'/0'/0'/2']{XPUB}/<0;1>/*,"
        f"[ab12cd34/48'/0'/0'/2']{XPUB}/<0;1>/*,[ab12cd34/48'/0'/0'/2']{XPUB}/<0;1>/*))"
    )
    assert derive_addresses(apostrophe, gap=1)[0]["address"] == together[0]["address"]
    assert classify_btc_key(external + "\n" + internal) == "multisig"
    masked = mask_key(external + "\n" + internal)
    assert XPUB not in masked
    assert external not in masked


def test_single_sig_forms_and_checksum():
    wpkh = add_checksum(f"wpkh({XPUB}/<0;1>/*)")
    nested = add_checksum(f"sh(wpkh({XPUB}/0/*))")
    taproot = add_checksum(f"tr({XPUB}/<0;1>/*)")
    assert derive_addresses(wpkh, gap=1)[0]["address"].startswith("bc1q")
    assert derive_addresses(nested, gap=1)[0]["address"].startswith("3")
    assert derive_addresses(taproot, gap=1)[0]["address"].startswith("bc1p")
    broken = wpkh[:-1] + ("p" if wpkh[-1] != "p" else "q")
    with pytest.raises(ValueError, match="checksum"):
        derive_addresses(broken, gap=1)


def test_private_keys_are_rejected_without_echo():
    wrapped = f"wpkh({XPRV}/0/*)"
    wif = "5" + ("1" * 50)
    tprv = "tprv" + ("1" * 30)
    for secret in (wrapped, wif, tprv):
        with pytest.raises(RejectedSecretError, match="chiave privata") as caught:
            classify_btc_key(secret)
        assert XPRV not in str(caught.value)
        assert wif not in str(caught.value)


def test_snapshot_counts_bitcoin_without_yield(tmp_path, monkeypatch):
    from app import config, db, portfolio

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "monitor.db")
    monkeypatch.setattr(config, "_READY", True)
    monkeypatch.setattr(config, "DATA_PERSISTENT", True)
    portfolio.invalidate()
    db.init_db()
    db.cache_set(
        "portfolio",
        {
            "ts": 1_700_000_000,
            "rpc_ok": True,
            "errors": [],
            "positions": [{"owner": "io", "protocol": "Aave", "asset": "USDC", "usd": 1_000, "apy": 10, "apy30": 10}],
            "switches": [],
            "security": [],
            "btc_wallets": [
                {
                    "owner": "moglie",
                    "label": "Bitkey",
                    "btc": 2,
                    "kind": "multisig",
                    "key_masked": "wsh(sort…abcd1234",
                    "connected": True,
                    "txs": [],
                }
            ],
            "btc_security": [],
            "btc_price_usd": 50,
            "fx_thb": 35,
            "gas_usd": 0,
            "eth_price_usd": 1,
            "node": {"configured": True, "connected": True, "detail": "ok"},
            "alternatives": [],
            "evm_owners": ["io"],
            "wallet_count": 1,
            "ens_names": [],
            "ens_alerts": [],
            "saved": True,
        },
    )
    portfolio.invalidate()
    try:
        view = asyncio.run(portfolio.snapshot("tutti", force=False))
    finally:
        portfolio.invalidate()
    assert view["income"]["io"]["year_usd"] == pytest.approx(100)
    assert view["income"]["moglie"]["btc"] == pytest.approx(2)
    assert view["income"]["moglie"]["btc_usd"] == pytest.approx(100)
    assert view["income"]["tutti"]["btc_apy"] == 0
    assert view["totals"]["tutti"]["total_usd"] == pytest.approx(1_100)
    assert view["btc_wallets"][0]["usd"] == pytest.approx(100)
    assert "descriptor" not in view["btc_wallets"][0]


def test_bitcoin_does_not_earn_yield():
    figures = income_figures([{"protocol": "Aave", "usd": 1_000, "apy": 10, "apy30": 10}])
    assert figures["year_usd"] == pytest.approx(100)
    figures["btc_usd"] = 9_000
    figures["btc_apy"] = 0
    assert figures["year_usd"] == pytest.approx(100)


def test_electrs_missing_names_the_umbrel_host(monkeypatch):
    from app import bitcoin, config, db

    monkeypatch.setattr(config, "DATA_DIR", config.DATA_DIR)
    monkeypatch.setattr(bitcoin, "_DETECTED_AT", 0.0)
    monkeypatch.setattr(bitcoin, "_DETECTED", None)

    def nowhere(*_args, **_kwargs):
        raise socket.gaierror("assente")

    monkeypatch.setattr(bitcoin.socket, "getaddrinfo", nowhere)
    monkeypatch.setattr(db, "get_setting", lambda key, default="": "" if key == "electrs_host" else "50001")
    status = asyncio.run(electrs_status())
    assert status["connected"] is False
    assert "electrs_electrs_1" in status["detail"]


def test_saved_descriptor_stays_masked_and_survives_restart(tmp_path, monkeypatch):
    from app import config, db

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "monitor.db")
    monkeypatch.setattr(config, "_READY", True)
    monkeypatch.setattr(config, "DATA_PERSISTENT", True)
    db.init_db()
    text = _wsh("<0;1>/*")
    db.add_btc("Bitkey", "multisig", text, "moglie")
    db.init_db()
    saved = db.list_btc()
    assert len(saved) == 1
    assert saved[0]["descriptor"] == text
    backup = db.export_backup()
    assert "sola osservazione" in backup["warning"]
    assert backup["btc"][0]["descriptor"] == text
    shown = mask_key(saved[0]["descriptor"])
    assert text not in shown
