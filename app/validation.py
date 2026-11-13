"""Reject private keys and seeds. Accept only public addresses and xpubs/descriptors."""
from __future__ import annotations

import json
import re
from typing import Any

OWNER_VALUES = {"io", "moglie"}
OWNER_FILTERS = {"io", "moglie", "tutti"}

PRIVATE_RE = re.compile(
    r"^(xprv|yprv|zprv|tprv|uprv|vprv)[1-9A-HJ-NP-Za-km-z]{20,}$",
    re.IGNORECASE,
)
HEX_PRIVKEY = re.compile(r"^(0x)?[0-9a-fA-F]{64}$")
WIF_RE = re.compile(r"^[5KL9c][1-9A-HJ-NP-Za-km-z]{50,51}$")
WIF_STANDALONE = re.compile(r"(?:^|\s)[5KL][1-9A-HJ-NP-Za-km-z]{50,51}(?:\s|$)")
BASE58_TOKEN = re.compile(r"[1-9A-HJ-NP-Za-km-z]+")
EXTENDED_KEY = re.compile(r"(?i)(?:^|[^A-Za-z0-9])([xtyzuv](?:pub|prv))[1-9A-HJ-NP-Za-km-z]{20,}")
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


def _has_extended_private(text: str) -> bool:
    return any(match.group(1).lower().endswith("prv") for match in EXTENDED_KEY.finditer(text or ""))


def _has_wif(text: str) -> bool:
    for match in BASE58_TOKEN.finditer(text or ""):
        token = match.group(0)
        if EXTENDED_KEY.search(token):
            continue
        if WIF_RE.match(token):
            return True
    return False


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
    if DESC_PRIV.search(value) or _has_extended_private(value):
        return True
    if WIF_STANDALONE.search(value) or _has_wif(value):
        return True
    if "mnemonic" in low and len(low.split()) >= 12:
        return True
    return False


def reject_if_secret(text: str) -> None:
    if not looks_like_secret(text):
        return
    if _has_extended_private(text) or _has_wif(text) or PRIVATE_RE.match((text or "").strip()):
        raise RejectedSecretError(
            "Questa riga contiene una chiave privata (xprv, tprv o WIF). "
            "L'app accetta solo indirizzi Ethereum pubblici. "
            "La chiave privata non viene salvata e non va mai incollata qui."
        )
    raise RejectedSecretError(
        "Sembra una chiave privata o una seed. Questa app è in sola lettura: "
        "incolla solo un indirizzo Ethereum pubblico. "
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
    first = next((line.strip() for line in value.splitlines() if line.strip()), "")
    lowered = value.lower()
    if "sortedmulti(" in lowered or "multi(" in lowered:
        return "multisig"
    if DESC_HINT.match(first) or first.lower().startswith("wpkh(") or first.lower().startswith("tr("):
        return "descriptor"
    if XPUB_RE.match(value):
        return value[:4].lower()
    raise ValueError(
        "Chiave Bitcoin non riconosciuta. Accettiamo xpub, ypub, zpub o un descrittore di sola osservazione."
    )


def address_row(address: str, owner: str, label: str = "") -> dict[str, str]:
    """One public address. Secrets are refused and never returned."""
    reject_if_secret(address)
    reject_if_secret(label)
    clean = normalize_eth(address)
    who = normalize_owner(owner)
    text = (label or "").strip()
    if not text:
        text = "Importato"
    return {"address": clean, "owner": who, "label": text[:80]}


def _row_from_line(line: str) -> dict[str, str]:
    """`address, io|moglie, label`. The label is optional and may contain commas."""
    reject_if_secret(line)
    parts = [part.strip() for part in line.split(",")]
    if len(parts) < 2 or not parts[0] or not parts[1]:
        raise ValueError("formato: indirizzo, io|moglie, etichetta.")
    return address_row(parts[0], parts[1], ",".join(parts[2:]))


def _row_from_item(item: Any) -> dict[str, str]:
    if isinstance(item, str):
        return _row_from_line(item)
    if not isinstance(item, dict):
        raise ValueError("voce non riconosciuta.")
    return address_row(
        str(item.get("address") or ""),
        str(item.get("owner") or ""),
        str(item.get("label") or ""),
    )


def _inputs_from_text(text: str) -> list[tuple[str, Any]]:
    stripped = text.strip().lstrip("\ufeff")
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, (dict, list)):
            return _inputs_from_body(parsed)
    found: list[tuple[str, Any]] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        raw = line.strip()
        if not raw or raw.startswith("#"):
            continue
        found.append((f"Riga {lineno}", raw))
    return found


def _inputs_from_body(body: Any) -> list[tuple[str, Any]]:
    if isinstance(body, str):
        return _inputs_from_text(body)
    if isinstance(body, list):
        return [(f"Voce {index}", item) for index, item in enumerate(body, 1)]
    if isinstance(body, dict):
        blob = body.get("addresses")
        if not isinstance(blob, list):
            blob = body.get("evm") if isinstance(body.get("evm"), list) else None
        found: list[tuple[str, Any]] = []
        if isinstance(blob, list):
            found.extend((f"Voce {index}", item) for index, item in enumerate(blob, 1))
        text = body.get("text")
        if isinstance(text, str) and text.strip():
            found.extend(_inputs_from_text(text))
        return found
    raise ValueError("Il file non è un elenco di indirizzi.")


def prepare_address_import(body: Any) -> tuple[list[dict[str, str]], list[str]]:
    """Valid rows plus short Italian errors. Errors never repeat the pasted text."""
    inputs = _inputs_from_body(body)
    rows: list[dict[str, str]] = []
    errors: list[str] = []
    for origin, item in inputs:
        try:
            rows.append(_row_from_item(item))
        except RejectedSecretError:
            errors.append(f"{origin}: rifiutata, sembra una chiave o una seed.")
        except ValueError as exc:
            message = str(exc).split("\n", 1)[0]
            errors.append(f"{origin}: {message}")
    return rows, errors
