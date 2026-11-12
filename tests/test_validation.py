
import pytest
from app.validation import (
    RejectedSecretError,
    classify_btc_key,
    looks_like_secret,
    normalize_eth,
    reject_if_secret,
)

SEED_12 = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"
# Well-known xprv prefix test string (not used as a live key, only to verify rejection).
XPRV = "xprv9s21ZrQH143K3QTDL4LXw2F7HEK3wJUD2nW2nRk4stbPy6cq3jPPqjiChkVvvNKmPGJxWUtg6LnF5kejMRNNU3TGtRBeJgk33yuGBxrMPHi"


def test_rejects_seed():
    assert looks_like_secret(SEED_12)
    with pytest.raises(RejectedSecretError):
        reject_if_secret(SEED_12)


def test_rejects_xprv():
    assert looks_like_secret(XPRV)
    with pytest.raises(RejectedSecretError):
        classify_btc_key(XPRV)


def test_rejects_hex_priv():
    with pytest.raises(RejectedSecretError):
        reject_if_secret("0x" + "ab" * 32)


def test_accepts_eth():
    addr = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"
    assert normalize_eth(addr) == addr.lower()


def test_rejects_bad_eth():
    with pytest.raises(ValueError):
        normalize_eth("d8dA6BF26964aF9D7eEd9e03E53415D37aA96045")


def test_classifies_xpub():
    xpub = "xpub661MyMwAqRbcFhCvdhTAfpEEDV58oqDvv65YNHC686NNs4KbH8YZQJWVmrfbve7aAVHzxw8bKFxA7MLeDK6BbLfkE3bqkvHLPgaGHHtYGeY"
    assert classify_btc_key(xpub) == "xpub"
