
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


def test_bitkey_branches_match_explicit_paths_and_core():
    """Receive stays the 0.2.8 path. Change is added beside it, not instead of it."""
    import hashlib

    from embit.descriptor import Descriptor
    from embit.descriptor.checksum import add_checksum
    from embit.networks import NETWORKS

    from app.bitcoin import (
        _chain_role,
        _parse_public,
        _scripthash,
        branch_plans,
        derive_addresses,
        prepare_watch_descriptor,
        verify_checksum,
    )

    origin = "[ab12cd34/48h/0h/0h/2h]"
    key = f"{origin}{XPUB}"

    def explicit(path: str) -> str:
        body = f"wsh(sortedmulti(2,{key}/{path},{key}/{path},{key}/{path}))"
        return Descriptor.from_string(body).derive(0).address(NETWORKS["main"])

    multipath = add_checksum(f"wsh(sortedmulti(2,{key}/<0;1>/*,{key}/<0;1>/*,{key}/<0;1>/*))")
    external = add_checksum(f"wsh(sortedmulti(2,{key}/0/*,{key}/0/*,{key}/0/*))")
    internal = add_checksum(f"wsh(sortedmulti(2,{key}/1/*,{key}/1/*,{key}/1/*))")
    receive_ref = explicit("0/0")
    change_ref = explicit("1/0")
    doubled = explicit("0/0/0")

    def roles(text: str) -> dict[str, str]:
        return {item["role"]: item["address"] for item in derive_addresses(text, gap=1)}

    together = roles(multipath)
    separate = roles(external + "\n" + internal)
    already = roles(external)
    assert together == separate == {"receive": receive_ref, "change": change_ref}
    assert already == together
    assert receive_ref != doubled
    assert roles(multipath.split("#")[0]) == together

    def legacy_receive(raw: str, gap: int) -> list[str]:
        """The 0.2.8 loop: one branch keeps its role, a multipath uses derive(index, branch)."""
        found: list[str] = []
        seen: set[str] = set()
        for line in prepare_watch_descriptor(raw).split("\n"):
            body = verify_checksum(line)
            kind = classify_btc_key(body)
            desc = _parse_public(body, kind)
            forced = _chain_role(body) if kind in {"descriptor", "multisig"} else None
            branches = getattr(desc, "num_branches", 1) or 1
            for branch in range(branches):
                role = forced if forced and branches == 1 else {0: "receive", 1: "change"}.get(branch, f"branch{branch}")
                if role != "receive":
                    continue
                for index in range(gap):
                    derived = desc.derive(index, branch)
                    addr = derived.address(NETWORKS["main"])
                    if addr not in seen:
                        seen.add(addr)
                        found.append(addr)
        return found

    current_receive = [item["address"] for item in derive_addresses(external, gap=2) if item["role"] == "receive"]
    assert current_receive == legacy_receive(external, 2)
    assert [item["address"] for item in derive_addresses(external, gap=2) if item["role"] == "change"]
    assert {item[0] for item in branch_plans(external)} == {"receive", "change"}

    core = (
        "wsh(sortedmulti(2,xpub6ERApfZwUNrhLCkDtcHTcxd75RbzS1ed54G1LkBUHQVHQKqhMkhgbmJbZRkrgZw4koxb5JaHWkY4ALHY2grBGRjaDMzQLcgJvLJuZZvRcEL/*"
        ",xpub68NZiKmJWnxxS6aaHmn81bvJeTESw724CRDs6HbuccFQN9Ku14VQrADWgqbhhTHBaohPX4CjNLf9fq9MYo6oDaPPLPxSb7gwQN3ih19Zm4Y/0/0/*))"
    )
    reference = Descriptor.from_string(core).derive(0)
    witness = reference.witness_script().data.hex()
    assert witness == (
        "5221025d5fc65ebb8d44a5274b53bac21ff8307fec2334a32df05553459f8b1f7fe1b6"
        "2102fbd47cc8034098f0e6a94c6aeee8528abf0a2153a5d8e46d325b7284c046784652ae"
    )
    got = next(item for item in derive_addresses(core, gap=1) if item["role"] == "receive" and item["index"] == 0)
    assert got["scriptpubkey"] == reference.script_pubkey().data.hex()
    digest = hashlib.sha256(bytes.fromhex(got["scriptpubkey"])).digest()
    assert _scripthash(got["scriptpubkey"]) == digest[::-1].hex()
    assert _scripthash(got["scriptpubkey"]) != digest.hex()


def test_mask():
    masked = mask_key(XPUB)
    assert "…" in masked
    assert XPUB not in masked
    assert classify_btc_key(XPUB) == "xpub"
