"""EIP-55 checksum without a third-party keccak dependency."""
from __future__ import annotations


def keccak256(data: bytes) -> bytes:
    """Keccak-f[1600], rate 136, domain pad 0x01 (not NIST SHA3)."""
    rate = 136
    rc = [
        0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000,
        0x000000000000808B, 0x0000000080000001, 0x8000000080008081, 0x8000000000008009,
        0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
        0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
        0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
        0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
    ]
    rotc = [1, 3, 6, 10, 15, 21, 28, 36, 45, 55, 2, 14, 27, 41, 56, 8, 25, 43, 62, 18, 39, 61, 20, 44]
    piln = [10, 7, 11, 17, 18, 3, 5, 16, 8, 21, 24, 4, 15, 23, 19, 13, 12, 2, 20, 14, 22, 9, 6, 1]
    state = [0] * 25

    def rot64(value: int, shift: int) -> int:
        shift %= 64
        return ((value << shift) | (value >> (64 - shift))) & ((1 << 64) - 1)

    padded = bytearray(data) + bytes([0x01])
    while len(padded) % rate:
        padded.append(0)
    padded[-1] |= 0x80
    for offset in range(0, len(padded), rate):
        block = padded[offset:offset + rate]
        for i in range(rate // 8):
            state[i] ^= int.from_bytes(block[i * 8:(i + 1) * 8], "little")
        for rnd in range(24):
            columns = [state[i] ^ state[i + 5] ^ state[i + 10] ^ state[i + 15] ^ state[i + 20] for i in range(5)]
            for i in range(5):
                mixed = columns[(i + 4) % 5] ^ rot64(columns[(i + 1) % 5], 1)
                for j in range(0, 25, 5):
                    state[j + i] ^= mixed
            current = state[1]
            for i in range(24):
                j = piln[i]
                state[j], current = rot64(current, rotc[i]), state[j]
            for j in range(0, 25, 5):
                t0, t1, t2, t3, t4 = state[j:j + 5]
                state[j + 0] ^= (~t1) & t2
                state[j + 1] ^= (~t2) & t3
                state[j + 2] ^= (~t3) & t4
                state[j + 3] ^= (~t4) & t0
                state[j + 4] ^= (~t0) & t1
            state[0] ^= rc[rnd]
    out = bytearray()
    for i in range(4):
        out += state[i].to_bytes(8, "little")
    return bytes(out[:32])


def to_checksum(address: str) -> str:
    body = address.lower().removeprefix("0x")
    digest = keccak256(body.encode()).hex()
    chars = [c.upper() if int(digest[i], 16) >= 8 else c for i, c in enumerate(body)]
    return "0x" + "".join(chars)
