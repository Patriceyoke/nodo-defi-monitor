"""Reject private keys and seeds. Accept only public addresses and xpubs/descriptors."""
from __future__ import annotations

import re

OWNER_VALUES = {"io", "moglie"}
OWNER_FILTERS = {"io", "moglie", "tutti"}

PRIVATE_RE = re.compile(
    r"^(xprv|yprv|zprv|tprv|uprv|vprv)[1-9A-HJ-NP-Za-km-z]{20,}$",
    re.IGNORECASE,
)
HEX_PRIVKEY = re.compile(r"^(0x)?[0-9a-fA-F]{64}$")
WIF_RE = re.compile(r"^[5KL9c][1-9A-HJ-NP-Za-km-z]{50,51}$")
WIF_STANDALONE = re.compile(r"(?:^|\s)[5KL][1-9A-HJ-NP-Za-km-z]{50,51}(?:\s|$)")
BIP39_HINT = re.compile(r"^[a-z]+(?:\s+[a-z]+){11,23}$")
ETH_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
XPUB_RE = re.compile(r"^[xyzXYZ]pub[1-9A-HJ-NP-Za-km-z]{20,}$")
DESC_HINT = re.compile(
    r"^(pkh|wpkh|sh|wsh|tr|combo|addr|raw|multi|sortedmulti)\(",
    re.IGNORECASE,
)
DESC_PRIV = re.compile(
    r"(?:^|[^A-Za-z0-9])(xprv|yprv|zprv|tprv|uprv|vprv)[1-9A-HJ-NP-Za-km-z]{20,}",
    re.IGNORECASE,
)


class RejectedSecretError(ValueError):
    """Raised when the user pastes a key or seed that this app must never accept."""


def looks_like_secret(text: str) -> bool:
    value = (text or "").strip()
    if not value:
        return False
    if XPUB_RE.match(value) and not DESC_PRIV.search(value):
        return False
    low = " ".join(value.lower().split())
    if PRIVATE_RE.match(low) or HEX_PRIVKEY.match(low) or WIF_RE.match(value):
        return True
    if BIP39_HINT.match(low) and len(low.split()) in {12, 15, 18, 21, 24}:
        return True
    if DESC_PRIV.search(value):
        return True
    if WIF_STANDALONE.search(value):
        return True
    if "mnemonic" in low and len(low.split()) >= 12:
        return True
    return False


def reject_if_secret(text: str) -> None:
    if looks_like_secret(text):
        raise RejectedSecretError(
            "Sembra una chiave privata o una seed. Questa app è in sola lettura: "
            "incolla solo un indirizzo Ethereum pubblico, un xpub, uno zpub o un descrittore di sola osservazione. "
            "Non incollare mai seed o chiavi private."
        )


def normalize_owner(owner: str, allow_all: bool = False) -> str:
    value = (owner or "").strip().lower()
    allowed = OWNER_FILTERS if allow_all else OWNER_VALUES
    if value not in allowed:
        raise ValueError(
            "Titolare non valido: usa io, moglie" + (" o tutti" if allow_all else "") + "."
        )
    return value


def normalize_eth(address: str) -> str:
    reject_if_secret(address)
    value = (address or "").strip()
    if not ETH_RE.match(value):
        raise ValueError(
            "Indirizzo Ethereum non valido. Serve un indirizzo pubblico 0x seguito da 40 caratteri esadecimali."
        )
    return value.lower()


def classify_btc_key(text: str) -> str:
    """Return 'xpub', 'ypub', 'zpub' or 'descriptor' after rejecting secrets."""
    reject_if_secret(text)
    value = (text or "").strip()
    if not value:
        raise ValueError("Inserisci un xpub, uno zpub o un descrittore di sola osservazione.")
    if DESC_HINT.match(value) or value.startswith("wpkh(") or "sortedmulti(" in value or "multi(" in value:
        return "descriptor"
    if XPUB_RE.match(value):
        return value[:4].lower()
    raise ValueError(
        "Chiave Bitcoin non riconosciuta. Accettiamo xpub, ypub, zpub o un descrittore di sola osservazione."
    )
