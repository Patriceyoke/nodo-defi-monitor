
from app.bitcoin import derive_addresses, mask_key
from app.validation import classify_btc_key

XPUB = "xpub661MyMwAqRbcFhCvdhTAfpEEDV58oqDvv65YNHC686NNs4KbH8YZQJWVmrfbve7aAVHzxw8bKFxA7MLeDK6BbLfkE3bqkvHLPgaGHHtYGeY"


def test_derives_receive_and_change():
    addrs = derive_addresses(XPUB, gap=3)
    assert len(addrs) == 6
    roles = {a["role"] for a in addrs}
    assert roles == {"receive", "change"}
    assert all(a["address"].startswith("1") or a["address"].startswith("bc1") or a["address"].startswith("3") for a in addrs)
    # Deterministic
    again = derive_addresses(XPUB, gap=3)
    assert [a["address"] for a in addrs] == [a["address"] for a in again]


def test_mask():
    masked = mask_key(XPUB)
    assert "…" in masked
    assert XPUB not in masked
    assert classify_btc_key(XPUB) == "xpub"
