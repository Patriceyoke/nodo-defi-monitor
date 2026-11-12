"""Watch-only Bitcoin: derive addresses from xpub/zpub/descriptors and query Electrs."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import socket
import ssl
import time
from typing import Any

from embit.descriptor import Descriptor
from embit.descriptor.checksum import checksum as descriptor_checksum
from embit.networks import NETWORKS

from .config import (
    DEFAULT_ELECTRS_HOST,
    DEFAULT_ELECTRS_PORT,
    DEFAULT_GAP_LIMIT,
    ELECTRS_CANDIDATES,
)
from . import db
from .validation import classify_btc_key, reject_if_secret

log = logging.getLogger("monitor.bitcoin")

# Version bytes we accept (mainnet public only).
PUB_VERSIONS = {
    b"\x04\x88\xb2\x1e": "xpub",  # xpub
    b"\x04\x9d\x7c\xb2": "ypub",  # ypub
    b"\x04\xb2\x47\x46": "zpub",  # zpub
}


class ElectrsError(RuntimeError):
    pass


_INDEX_RE = re.compile(r"index|sync|not ready|waiting", re.IGNORECASE)
_CHECKSUM_RE = re.compile(r"^[qpzry9x8gf2tvdw0s3jn54khce6mua7l]{8}$")
_DETECTED: tuple[str, int] | None = None
_DETECTED_AT = 0.0


def gap_limit() -> int:
    raw = db.get_setting("btc_gap_limit") or str(DEFAULT_GAP_LIMIT)
    try:
        value = int(raw)
    except ValueError:
        value = DEFAULT_GAP_LIMIT
    return max(1, min(value, 100))


def _configured_endpoint() -> tuple[str, int]:
    host = (db.get_setting("electrs_host") or DEFAULT_ELECTRS_HOST or "").strip()
    port_raw = db.get_setting("electrs_port") or str(DEFAULT_ELECTRS_PORT)
    try:
        port = int(port_raw)
    except ValueError:
        port = DEFAULT_ELECTRS_PORT
    return host, port


def _host_resolves(host: str) -> bool:
    try:
        socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        return True
    except socket.gaierror:
        return False


def detect_electrs() -> tuple[str, int] | None:
    """Find Umbrel Electrs when the user left the host empty. Cached for a minute."""
    global _DETECTED, _DETECTED_AT
    now = time.time()
    if now - _DETECTED_AT < 60:
        return _DETECTED
    found: tuple[str, int] | None = None
    for host, port in ELECTRS_CANDIDATES:
        if not _host_resolves(host):
            continue
        try:
            _electrum_request(host, port, "server.version", ["monitor-defi", "1.4"], timeout=1.5)
            found = (host, port)
            break
        except Exception:
            continue
    _DETECTED = found
    _DETECTED_AT = now
    return found


def electrs_endpoint() -> tuple[str, int, str]:
    """Return host, port and how it was chosen: configured, detected or missing."""
    host, port = _configured_endpoint()
    if host:
        return host, port, "configured"
    detected = detect_electrs()
    if detected:
        return detected[0], detected[1], "detected"
    return "", port, "missing"


def mask_key(text: str) -> str:
    value = (text or "").strip()
    if len(value) <= 18:
        return value[:6] + "…"
    return value[:8] + "…" + value[-8:]


def descriptor_lines(raw: str) -> list[str]:
    """One descriptor, or the external line plus the internal line."""
    text = (raw or "").replace("\r\n", "\n").strip()
    if not text:
        return []
    chunks: list[str] = []
    buf = ""
    depth = 0
    for char in text:
        if char == "\n" and depth == 0:
            if buf.strip():
                chunks.append(buf.strip())
            buf = ""
            continue
        buf += char
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
    if buf.strip():
        chunks.append(buf.strip())
    merged: list[str] = []
    for chunk in chunks:
        if chunk.startswith("#") and merged:
            merged[-1] += chunk
        else:
            merged.append(chunk)
    return merged


def verify_checksum(line: str) -> str:
    body, sep, check = line.partition("#")
    body = body.strip()
    if not sep:
        return body
    check = check.strip()
    if not _CHECKSUM_RE.match(check):
        raise ValueError("Il checksum del descrittore non è valido. Copialo per intero, con i caratteri dopo #.")
    try:
        expect = descriptor_checksum(body)
    except Exception:
        raise ValueError("Il descrittore contiene un carattere che non entra nel checksum.") from None
    if check != expect:
        raise ValueError("Il checksum del descrittore non corrisponde. Controlla di aver copiato la riga per intero.")
    return body


def _chain_role(body: str) -> str | None:
    if "<0;1>" in body or "{0,1}" in body:
        return None
    if "/0/*" in body or body.endswith("/0/*"):
        return "receive"
    if "/1/*" in body or body.endswith("/1/*"):
        return "change"
    return None


def _parse_public(body: str, kind: str) -> Descriptor:
    if kind in {"descriptor", "multisig"}:
        expr = body
    elif kind == "zpub":
        expr = f"wpkh({body}/<0;1>/*)"
    elif kind == "ypub":
        expr = f"sh(wpkh({body}/<0;1>/*))"
    else:
        expr = f"pkh({body}/<0;1>/*)"
    try:
        return Descriptor.from_string(expr)
    except Exception:
        raise ValueError(
            "Descrittore non valido. Servono wpkh, sh(wpkh), tr oppure wsh(sortedmulti(2,…)), "
            "con il percorso /0/*, /1/* o /<0;1>/*, e solo chiavi pubbliche."
        ) from None


def derive_addresses(raw: str, gap: int | None = None) -> list[dict[str, Any]]:
    """Derive receive (/0) and change (/1) addresses. Never touches private keys."""
    reject_if_secret(raw)
    if gap is None:
        gap = gap_limit()
    gap = max(1, min(int(gap), 100))
    lines = descriptor_lines(raw)
    if not lines:
        raise ValueError("Inserisci un xpub, uno zpub o un descrittore di sola osservazione.")
    if len(lines) > 2:
        raise ValueError("Incolla un solo descrittore, oppure la riga esterna (/0/*) e quella interna (/1/*).")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line in lines:
        body = verify_checksum(line)
        kind = classify_btc_key(body)
        desc = _parse_public(body, kind)
        forced = _chain_role(body) if kind in {"descriptor", "multisig"} else None
        branches = getattr(desc, "num_branches", 1) or 1
        for branch in range(branches):
            role = forced if forced and branches == 1 else {0: "receive", 1: "change"}.get(branch, f"branch{branch}")
            for index in range(gap):
                try:
                    derived = desc.derive(index, branch)
                except TypeError:
                    derived = desc.derive(index)
                addr = derived.address(NETWORKS["main"])
                if addr in seen:
                    continue
                seen.add(addr)
                out.append(
                    {
                        "address": addr,
                        "scriptpubkey": derived.script_pubkey().data.hex(),
                        "branch": 1 if role == "change" else branch,
                        "index": index,
                        "role": role,
                    }
                )
    if not out:
        raise ValueError("Impossibile derivare indirizzi da questa chiave o da questo descrittore.")
    return out


def _scripthash(script_hex: str) -> str:
    raw = bytes.fromhex(script_hex)
    digest = hashlib.sha256(raw).digest()
    return digest[::-1].hex()


def _electrum_request(host: str, port: int, method: str, params: list[Any], timeout: float = 8.0) -> Any:
    payload = json.dumps({"id": 1, "jsonrpc": "2.0", "method": method, "params": params}) + "\n"
    sock = socket.create_connection((host, port), timeout=timeout)
    try:
        # Electrs on Umbrel is usually plain TCP on 50001 (or SSL on 50002).
        if port in {50002, 443, 60002}:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            sock = ctx.wrap_socket(sock, server_hostname=host)
        sock.sendall(payload.encode("utf-8"))
        chunks: list[bytes] = []
        sock.settimeout(timeout)
        while True:
            data = sock.recv(65536)
            if not data:
                break
            chunks.append(data)
            if b"\n" in data:
                break
        raw = b"".join(chunks).decode("utf-8").strip()
        if not raw:
            raise ElectrsError("Risposta vuota da Electrs")
        msg = json.loads(raw.split("\n")[0])
        if msg.get("error"):
            raise ElectrsError(str(msg["error"]))
        return msg.get("result")
    finally:
        try:
            sock.close()
        except Exception:
            pass


async def electrs_call(method: str, params: list[Any]) -> Any:
    host, port, source = electrs_endpoint()
    if not host:
        raise ElectrsError(
            "Electrs non è configurato e electrs_electrs_1:50001 non risponde. "
            "Installa Electrs su Umbrel oppure indica host e porta in Impostazioni."
        )
    try:
        return await asyncio.to_thread(_electrum_request, host, port, method, params)
    except Exception as exc:
        if source == "detected":
            global _DETECTED_AT
            _DETECTED_AT = 0.0
        raise exc


async def electrs_status() -> dict[str, Any]:
    host, port, source = electrs_endpoint()
    base = {
        "configured": bool(host),
        "detected": source == "detected",
        "connected": False,
        "indexing": False,
        "host": host,
        "port": port,
        "source": "Nodo Bitcoin Umbrel / Electrs (locale)",
    }
    if not host:
        return {
            **base,
            "detail": (
                "Electrs non è raggiungibile. Su Umbrel l'host di solito è electrs_electrs_1 "
                "e la porta 50001. Lascia l'host vuoto per cercarlo, oppure scrivilo in Impostazioni."
            ),
        }
    try:
        await electrs_call("server.version", ["monitor-defi", "1.4"])
        height = None
        try:
            headers = await electrs_call("blockchain.headers.subscribe", [])
            if isinstance(headers, dict):
                height = headers.get("height")
        except Exception as exc:  # noqa: BLE001
            if _INDEX_RE.search(str(exc)):
                return {
                    **base,
                    "indexing": True,
                    "height": height,
                    "detail": "Electrs risponde ma sta ancora indicizzando la blockchain. I saldi arriveranno quando l'indice è pronto.",
                }
            raise
        indexing = height in (None, 0)
        if not indexing:
            try:
                await electrs_call("blockchain.scripthash.get_balance", ["00" * 32])
            except Exception as exc:  # noqa: BLE001
                if _INDEX_RE.search(str(exc)):
                    indexing = True
                else:
                    raise
        if indexing:
            return {
                **base,
                "indexing": True,
                "height": height,
                "detail": "Electrs è collegato ma sta ancora indicizzando. I saldi possono essere incompleti finché l'indice non è pronto.",
            }
        where = f"{host}:{port}"
        how = "rilevato da Umbrel" if source == "detected" else "impostato da te"
        return {
            **base,
            "configured": True,
            "connected": True,
            "height": height,
            "detail": f"Connesso a Electrs ({where}, {how}). Altezza blocco {height}.",
        }
    except Exception as exc:  # noqa: BLE001
        return {
            **base,
            "detail": f"Electrs non è raggiungibile su {host}:{port}. {exc}",
        }


async def scan_wallet(wallet: dict[str, Any], gap: int | None = None) -> dict[str, Any]:
    try:
        addrs = derive_addresses(wallet["descriptor"], gap=gap)
    except Exception as exc:
        log.warning("Derivazione Bitcoin non riuscita (%s).", type(exc).__name__)
        raise
    status = await electrs_status()
    offline = "Electrs sta ancora indicizzando" if status.get("indexing") else "Electrs non raggiungibile"
    if not status["connected"]:
        return {
            "id": wallet["id"],
            "label": wallet["label"],
            "owner": wallet["owner"],
            "kind": wallet["kind"],
            "role": wallet.get("role") or "watch",
            "key_masked": mask_key(wallet["descriptor"]),
            "btc": 0.0,
            "sats": 0,
            "utxo_count": 0,
            "status": offline,
            "last": status.get("detail") or "Nessun dato: Electrs non è collegato.",
            "connected": False,
            "addresses": [],
            "txs": [],
            "reuse": [],
            "dust": [],
            "alert_out_btc": wallet.get("alert_out_btc"),
            "alert_min_btc": wallet.get("alert_min_btc"),
            "hot_threshold_btc": wallet.get("hot_threshold_btc"),
            "node": status,
        }

    used: list[dict[str, Any]] = []
    txs: list[dict[str, Any]] = []
    total = 0
    utxo_count = 0
    reuse: list[str] = []
    dust: list[dict[str, Any]] = []

    for item in addrs:
        sh = _scripthash(item["scriptpubkey"])
        try:
            raw_bal = await electrs_call("blockchain.scripthash.get_balance", [sh])
            hist = await electrs_call("blockchain.scripthash.get_history", [sh])
            unspent = await electrs_call("blockchain.scripthash.listunspent", [sh])
        except Exception as exc:  # noqa: BLE001
            if _INDEX_RE.search(str(exc)):
                status = {**status, "indexing": True, "connected": False, "detail": str(exc)}
                break
            log.warning("Lettura Electrs non riuscita (%s).", type(exc).__name__)
            continue
        if isinstance(unspent, list):
            utxo_count += len(unspent)
        confirmed = int((raw_bal or {}).get("confirmed") or 0)
        unconfirmed = int((raw_bal or {}).get("unconfirmed") or 0)
        sats = confirmed + unconfirmed
        history = list(hist or [])
        if sats == 0 and not history:
            continue
        if len(history) > 1 and item["role"] == "receive":
            reuse.append(item["address"])
        total += sats
        used.append({**item, "sats": sats, "tx_count": len(history)})
        for ev in history:
            txs.append(
                {
                    "txid": ev.get("tx_hash"),
                    "height": ev.get("height") or 0,
                    "address": item["address"],
                    "role": item["role"],
                }
            )
        if sats and 0 < sats <= 1000:
            dust.append({"address": item["address"], "sats": sats})

    # Fetch a few recent transactions to classify incoming/outgoing roughly via status list
    txs.sort(key=lambda t: t.get("height") or 0, reverse=True)
    recent: list[dict[str, Any]] = []
    seen_tx: set[str] = set()
    for ev in txs:
        txid = ev.get("txid")
        if not txid or txid in seen_tx:
            continue
        seen_tx.add(txid)
        try:
            raw_tx = await electrs_call("blockchain.transaction.get", [txid, True])
        except Exception:
            raw_tx = None
        direction = "Ricevuto"
        amount_sats = 0
        if isinstance(raw_tx, dict):
            our_scripts = {a["scriptpubkey"] for a in used}
            incoming = 0
            outgoing = 0
            for vout in raw_tx.get("vout") or []:
                spk = (vout.get("scriptPubKey") or {}).get("hex") or ""
                val = vout.get("value")
                sats = int(round(float(val) * 100_000_000)) if isinstance(val, float) else int(val or 0)
                if spk in our_scripts:
                    incoming += sats
            # Electrum verbose tx may already be bitcoin-core style
            amount_sats = incoming
            if incoming == 0:
                direction = "Inviato"
                # best-effort: sum spent from our addresses is not always available without prevouts
                amount_sats = 0
            elif any(True for vin in (raw_tx.get("vin") or [])):
                # If the tx also spends our inputs it might be a send with change
                # Without prevouts we keep "Ricevuto" if value landed on our scripts and we cannot prove otherwise.
                pass
        recent.append(
            {
                "txid": txid,
                "height": ev.get("height") or 0,
                "kind": direction,
                "sats": amount_sats,
                "btc": amount_sats / 100_000_000,
                "address": ev.get("address"),
                "wallet_id": wallet["id"],
                "wallet": wallet["label"],
                "owner": wallet["owner"],
            }
        )
        if len(recent) >= 12:
            break

    if status.get("indexing"):
        total = 0
        utxo_count = 0
        used = []
        recent = []
    last = status.get("detail") if status.get("indexing") else "Nessun movimento visto nell'intervallo di derivazione."
    if recent:
        top = recent[0]
        btc = top["btc"]
        last = f"{top['kind']} {btc:.8f} BTC".rstrip("0").rstrip(".") + (f" · blocco {top['height']}" if top["height"] else "")

    return {
        "id": wallet["id"],
        "label": wallet["label"],
        "owner": wallet["owner"],
        "kind": wallet["kind"],
        "role": wallet.get("role") or "watch",
        "key_masked": mask_key(wallet["descriptor"]),
        "btc": total / 100_000_000,
        "sats": total,
        "utxo_count": utxo_count,
        "status": "Electrs sta ancora indicizzando" if status.get("indexing") else "Sincronizzato",
        "last": last,
        "connected": not status.get("indexing"),
        "addresses": used,
        "txs": recent,
        "reuse": reuse,
        "dust": dust,
        "alert_out_btc": wallet.get("alert_out_btc"),
        "alert_min_btc": wallet.get("alert_min_btc"),
        "hot_threshold_btc": wallet.get("hot_threshold_btc"),
        "node": status,
    }
