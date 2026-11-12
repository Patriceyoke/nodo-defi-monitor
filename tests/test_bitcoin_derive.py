
from app.bitcoin import branch_cap, consume_until_gap, derive_addresses, mask_key
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


def test_one_branch_still_scans_the_other():
    from embit.descriptor.checksum import add_checksum

    receive = add_checksum(f"wpkh({XPUB}/0/*)")
    both = derive_addresses(receive, gap=1)
    roles = {item["role"]: item["address"] for item in both}
    assert set(roles) == {"receive", "change"}
    change = add_checksum(f"wpkh({XPUB}/1/*)")
    other = {item["role"]: item["address"] for item in derive_addresses(change, gap=1)}
    assert roles == other


def test_branch_cap_stops_a_long_scan():
    assert branch_cap(None, 50) == 99
    assert branch_cap(10, 50) == 110
    assert branch_cap(980, 50) == 1000


def test_gap_extends_past_a_used_address_near_the_end():
    flags = [True] * 20 + [False] * 19 + [True] + [False] * 20
    seen, last = consume_until_gap(flags, 20)
    assert last == 39
    assert seen == 60


def test_sortedmulti_p2wsh_sorts_keys_bip67():
    from embit.descriptor import Descriptor
    from embit.descriptor.checksum import add_checksum
    from embit.bip32 import HDKey

    parent = HDKey.from_base58(XPUB)
    keys = [parent, parent.child(1), parent.child(0)]

    def expression(order):
        parts = ",".join(key.to_base58() + "/0/0" for key in order)
        return add_checksum(f"wsh(sortedmulti(2,{parts}))")

    forward = Descriptor.from_string(expression(keys)).derive(0)
    backward = Descriptor.from_string(expression(list(reversed(keys)))).derive(0)
    assert forward.address() == backward.address()
    script = bytes(forward.witness_script().data)
    pubs = []
    cursor = 0
    while cursor < len(script):
        op = script[cursor]
        cursor += 1
        if op == 33:
            pubs.append(script[cursor:cursor + 33])
            cursor += 33
    assert pubs == sorted(pubs)
    assert len(pubs) == 3
    assert forward.address().startswith("bc1q")


def test_mask():
    masked = mask_key(XPUB)
    assert "…" in masked
    assert XPUB not in masked
    assert classify_btc_key(XPUB) == "xpub"
