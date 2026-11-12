"""Watch-only Bitcoin: derive addresses from xpub/zpub/descriptors and query Electrs."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import socket
import ssl
from typing import Any

from embit import script
from embit.descriptor import Descriptor
from embit.networks import NETWORKS

from .config import DEFAULT_ELECTRS_HOST, DEFAULT_ELECTRS_PORT, DEFAULT_GAP_LIMIT
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


def electrs_endpoint() -> tuple[str, int]:
    host = (db.get_setting("electrs_host") or DEFAULT_ELECTRS_HOST or "").strip()
    port_raw = db.get_setting("electrs_port") or str(DEFAULT_ELECTRS_PORT)
    try:
        port = int(port_raw)
    except ValueError:
        port = DEFAULT_ELECTRS_PORT
    return host, port


def mask_key(text: str) -> str:
    value = (text or "").strip()
    if len(value) <= 18:
        return value[:6] + "…"
    return value[:8] + "…" + value[-8:]


def _to_descriptor(raw: str) -> Descriptor:
    reject_if_secret(raw)
    kind = classify_btc_key(raw)
    text = raw.strip()
    if kind == "descriptor":
        # Strip optional checksum suffix after #
        return Descriptor.from_string(text)
    # Convert an xpub/ypub/zpub into a standard single-sig descriptor.
    # ypub -> sh(wpkh(...)), zpub -> wpkh(...), xpub -> pkh(...)
    if kind == "zpub":
        expr = f"wpkh({text}/<0;1>/*)"
    elif kind == "ypub":
        expr = f"sh(wpkh({text}/<0;1>/*))"
    else:
        expr = f"pkh({text}/<0;1>/*)"
    return Descriptor.from_string(expr)


def derive_addresses(raw: str, gap: int = DEFAULT_GAP_LIMIT) -> list[dict[str, Any]]:
    """Derive receive (0) and change (1) addresses. Never touches private keys."""
    desc = _to_descriptor(raw)
    out: list[dict[str, Any]] = []
    n_branches = getattr(desc, "num_branches", 2) or 1
    roles = {0: "receive", 1: "change"}
    for branch in range(n_branches):
        role = roles.get(branch, f"branch{branch}")
        for index in range(gap):
            try:
                derived = desc.derive(index, branch)
            except TypeError:
                derived = desc.derive(index)
            addr = derived.address(NETWORKS["main"])
            scriptpubkey = derived.script_pubkey().data.hex()
            out.append(
                {
                    "address": addr,
                    "scriptpubkey": scriptpubkey,
                    "branch": branch,
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
    host, port = electrs_endpoint()
    if not host:
        raise ElectrsError("Indicizzatore Electrs/Fulcrum non configurato.")
    return await asyncio.to_thread(_electrum_request, host, port, method, params)


async def electrs_status() -> dict[str, Any]:
    host, port = electrs_endpoint()
    if not host:
        return {
            "configured": False,
            "connected": False,
            "host": "",
            "port": port,
            "source": "Nodo Bitcoin Umbrel / Electrs (locale)",
            "detail": "Nessun host Electrs o Fulcrum impostato. In Impostazioni indica l'indirizzo locale del tuo indicizzatore.",
        }
    try:
        banner = await electrs_call("server.banner", [])
        features = None
        try:
            features = await electrs_call("server.features", [])
        except Exception:
            features = None
        height = None
        if isinstance(features, dict):
            height = (features.get("hosts") and None) or features.get("genesis_hash")
        try:
            headers = await electrs_call("blockchain.headers.subscribe", [])
            if isinstance(headers, dict):
                height = headers.get("height", height)
        except Exception:
            pass
        return {
            "configured": True,
            "connected": True,
            "host": host,
            "port": port,
            "source": "Nodo Bitcoin Umbrel / Electrs (locale)",
            "detail": "Connesso all'indicizzatore locale.",
            "banner": banner if isinstance(banner, str) else None,
            "height": height,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "configured": True,
            "connected": False,
            "host": host,
            "port": port,
            "source": "Nodo Bitcoin Umbrel / Electrs (locale)",
            "detail": f"Indicizzatore non raggiungibile: {exc}",
        }


async def scan_wallet(wallet: dict[str, Any], gap: int = DEFAULT_GAP_LIMIT) -> dict[str, Any]:
    addrs = derive_addresses(wallet["descriptor"], gap=gap)
    status = await electrs_status()
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
            "status": "Indicizzatore non collegato",
            "last": "Nessun dato: Electrs/Fulcrum non è collegato.",
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
    reuse: list[str] = []
    dust: list[dict[str, Any]] = []

    for item in addrs:
        sh = _scripthash(item["scriptpubkey"])
        try:
            raw_bal = await electrs_call("blockchain.scripthash.get_balance", [sh])
            hist = await electrs_call("blockchain.scripthash.get_history", [sh])
        except Exception as exc:  # noqa: BLE001
            log.warning("scripthash query failed: %s", exc)
            continue
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

    last = "Nessun movimento visto nell'intervallo di derivazione."
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
        "status": "Sincronizzato",
        "last": last,
        "connected": True,
        "addresses": used,
        "txs": recent,
        "reuse": reuse,
        "dust": dust,
        "alert_out_btc": wallet.get("alert_out_btc"),
        "alert_min_btc": wallet.get("alert_min_btc"),
        "hot_threshold_btc": wallet.get("hot_threshold_btc"),
        "node": status,
    }
