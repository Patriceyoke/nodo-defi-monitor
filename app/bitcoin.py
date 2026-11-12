"""Watch-only Bitcoin: derive addresses from xpub/zpub/descriptors and query Electrs."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import socket
import ssl
import threading
import time
from typing import Any

from embit import base58
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
_INVISIBLE_RE = re.compile(r"[\u200b\u200c\u200d\u2060\ufeff]")
# Bitkey writes "External:" / "Internal:" in front of the descriptor. Also one pasted line.
_LABEL_RE = re.compile(
    r"""(?ix)
    (?:
        \A
        |
        [\r\n]+
        |
        (?<=[\s\)])
    )
    [^\S\r\n]*
    (?:[-*•·]\s*|\d+[.)]\s*)?
    [`"'“”‘’]*
    (?:external|internal|receive|receiving|change)
    [`"'“”‘’]*
    \s*:\s*
    """
)
_UNKNOWN_LABEL_RE = re.compile(r"(?i)^(?:[-*•·]\s*)?[`\"'“”‘’]*[a-z]{3,24}[`\"'“”‘’]*:")
_EXTPUB_RE = re.compile(r"(?i)[xtyzuv]pub[1-9A-HJ-NP-Za-km-z]{20,}")
# SLIP-132 public versions. The key material is what matters; embit derives from xpub/tpub.
_PUB_VERSIONS = {
    bytes.fromhex("0488b21e"): "main",  # xpub
    bytes.fromhex("049d7cb2"): "main",  # ypub
    bytes.fromhex("04b24746"): "main",  # zpub
    bytes.fromhex("0295b43f"): "main",  # Ypub
    bytes.fromhex("02aa7ed3"): "main",  # Zpub
    bytes.fromhex("043587cf"): "test",  # tpub
    bytes.fromhex("044a5262"): "test",  # upub
    bytes.fromhex("045f1cf6"): "test",  # vpub
    bytes.fromhex("024289ef"): "test",  # Upub
    bytes.fromhex("02575483"): "test",  # Vpub
}
_CANON_VERSION = {"main": bytes.fromhex("0488b21e"), "test": bytes.fromhex("043587cf")}
# host, port, via ("configured" or "host:port"), plain TCP
_WORKING: tuple[str, int, str, bool] | None = None
_WORKING_AT = 0.0
_WORKING_KEY = ""
_RESOLVE_LOCK = threading.Lock()
_WORKING_TTL = 60.0


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


def local_host_hint(host: str) -> str:
    """Names ending in .local are mDNS and do not resolve inside the container."""
    name = (host or "").strip().lower().rstrip(".")
    if name == "umbrel.local" or (name.endswith(".local") and name):
        return (
            "Questo nome .local non si risolve dentro l'app. "
            "Su Umbrel l'host è electrs_electrs_1 e la porta 50001. "
            "Puoi lasciarlo così: l'app prova da sola i contenitori Electrs e Fulcrum."
        )
    return ""


def _host_resolves(host: str) -> bool:
    try:
        socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        return True
    except socket.gaierror:
        return False


def _gateway_hosts() -> list[str]:
    """Docker host gateway, only when this process can name it."""
    found: list[str] = []
    if _host_resolves("host.docker.internal"):
        found.append("host.docker.internal")
    try:
        with open("/proc/net/route", encoding="ascii") as handle:
            for line in handle.readlines()[1:]:
                fields = line.split()
                if len(fields) < 3 or fields[1] != "00000000":
                    continue
                raw = fields[2]
                if len(raw) != 8:
                    continue
                ip = socket.inet_ntoa(bytes.fromhex(raw)[::-1])
                if ip and ip not in {"0.0.0.0", "127.0.0.1"} and ip not in found:
                    found.append(ip)
                break
    except OSError:
        pass
    return found


def _candidate_targets() -> list[tuple[str, int, str, bool]]:
    """Saved host first, then Umbrel container names, then the Docker gateway.

    Each item is host, port, via, plain TCP. Fallbacks are plain TCP.
    The saved host keeps TLS on 50002, which is how Electrs SSL is usually published.
    """
    host, port = _configured_endpoint()
    out: list[tuple[str, int, str, bool]] = []
    seen: set[tuple[str, int]] = set()

    def add(item_host: str, item_port: int, via: str, plain: bool) -> None:
        item_host = (item_host or "").strip()
        if not item_host or item_port <= 0:
            return
        key = (item_host, item_port)
        if key in seen:
            return
        seen.add(key)
        out.append((item_host, item_port, via, plain))

    if host:
        add(host, port, "configured", False)
    for candidate, candidate_port in ELECTRS_CANDIDATES:
        add(candidate, candidate_port, f"{candidate}:{candidate_port}", True)
    for gateway in _gateway_hosts():
        add(gateway, port or DEFAULT_ELECTRS_PORT, f"{gateway}:{port or DEFAULT_ELECTRS_PORT}", True)
        add(gateway, 50001, f"{gateway}:50001", True)
        add(gateway, 50002, f"{gateway}:50002", True)
    return out


def _forget_working() -> None:
    global _WORKING, _WORKING_AT, _WORKING_KEY
    _WORKING = None
    _WORKING_AT = 0.0
    _WORKING_KEY = ""


def resolve_electrs() -> tuple[str, int, str, bool]:
    """First Electrum server that answers server.version. Cached for a minute."""
    global _WORKING, _WORKING_AT, _WORKING_KEY
    configured_host, configured_port = _configured_endpoint()
    key = f"{configured_host}:{configured_port}"
    with _RESOLVE_LOCK:
        now = time.time()
        if _WORKING_KEY == key and now - _WORKING_AT < _WORKING_TTL:
            return _WORKING or ("", configured_port, "missing", True)
        found: tuple[str, int, str, bool] | None = None
        for host, port, via, plain in _candidate_targets():
            if not _host_resolves(host):
                continue
            try:
                _electrum_request(
                    host, port, "server.version", ["monitor-defi", "1.4"], timeout=1.5, plain=plain,
                )
            except Exception:
                log.info("Electrum %s:%s non risponde.", host, port)
                continue
            found = (host, port, via, plain)
            break
        _WORKING = found
        _WORKING_KEY = key
        _WORKING_AT = now
        if found:
            return found
        return "", configured_port, "missing", True


def electrs_endpoint() -> tuple[str, int, str]:
    """Return host, port and how it was chosen: configured, fallback or missing."""
    host, port, via, _plain = resolve_electrs()
    if not host:
        return "", port, "missing"
    if via == "configured":
        return host, port, "configured"
    return host, port, "fallback"


def mask_key(text: str) -> str:
    value = (text or "").strip()
    if len(value) <= 18:
        return value[:6] + "…"
    return value[:8] + "…" + value[-8:]


def _sanitize(raw: str) -> str:
    text = (raw or "").replace("\u00a0", " ").replace("\u202f", " ")
    text = _INVISIBLE_RE.sub("", text)
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def _compact(chunk: str) -> str:
    text = chunk.strip().strip("`\"'“”‘’")
    return re.sub(r"\s+", "", text)


def _split_depth(text: str) -> list[str]:
    """Keep a newline between two descriptors. Do not split inside parentheses."""
    chunks: list[str] = []
    buf = ""
    depth = 0
    for char in text:
        if char == "\n" and depth == 0:
            if buf.strip():
                chunks.append(buf)
            buf = ""
            continue
        buf += char
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
    if buf.strip():
        chunks.append(buf)
    return chunks


def descriptor_lines(raw: str) -> list[str]:
    """One descriptor, or External plus Internal, even on a single pasted line."""
    text = _sanitize(raw)
    if not text:
        return []
    lines: list[str] = []
    for part in _LABEL_RE.split(text):
        for piece in _split_depth(part):
            chunk = _compact(piece)
            if not chunk:
                continue
            if chunk.startswith("#") and lines:
                lines[-1] += chunk
                continue
            if _UNKNOWN_LABEL_RE.match(chunk):
                raise ValueError(
                    "Etichetta: non riconosciuta. Prima del descrittore puoi scrivere External, Internal, Receive o Change, poi i due punti."
                )
            lines.append(chunk)
    return lines


def verify_checksum(line: str) -> str:
    body, sep, check = line.partition("#")
    body = body.strip()
    if not sep:
        return body
    check = check.strip()
    if not _CHECKSUM_RE.match(check):
        raise ValueError("Checksum: il codice dopo # non è nel formato atteso.")
    try:
        expect = descriptor_checksum(body)
    except Exception:
        raise ValueError("Checksum: la riga contiene un carattere che non entra nel codice di controllo.") from None
    if check != expect:
        raise ValueError("Checksum: il codice dopo # non corrisponde. Controlla di aver copiato la riga per intero.")
    return body


def _canon_extpub(token: str) -> str:
    try:
        raw = base58.decode_check(token)
    except Exception:
        return token
    if len(raw) != 78 or raw[:4] not in _PUB_VERSIONS:
        return token
    target = _CANON_VERSION[_PUB_VERSIONS[raw[:4]]]
    if raw[:4] == target:
        return token
    return base58.encode_check(target + raw[4:])


def _canon_extpubs(body: str) -> str:
    return _EXTPUB_RE.sub(lambda match: _canon_extpub(match.group(0)), body)


def _parse_failure(exc: Exception) -> ValueError:
    low = str(exc).lower()
    if any(word in low for word in ("hardened", "derivation", "wildcard", "range", "index")):
        return ValueError(
            "Percorso: usa /0/* per la ricezione, /1/* per il resto, o /<0;1>/*, con ' oppure h per gli indici hardened."
        )
    if any(word in low for word in ("key", "base58", "version", "pubkey", "checksum", "xpub")):
        return ValueError("Chiave: serve una chiave pubblica xpub, Zpub o Vpub. Una chiave privata non è accettata.")
    return ValueError("Descrittore: formato non valido. Controlla etichetta, chiave, percorso o checksum.")


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
    except Exception as exc:
        raise _parse_failure(exc) from None


def prepare_watch_descriptor(raw: str) -> str:
    """Normalize a Bitkey export or a plain descriptor. The result has no labels and no private keys."""
    text = _sanitize(raw)
    reject_if_secret(text)
    lines = descriptor_lines(text)
    if not lines:
        raise ValueError("Etichetta: incolla il descrittore, oppure la riga External e la riga Internal.")
    if len(lines) > 2:
        raise ValueError("Etichetta: tieni solo la riga External e la riga Internal.")
    cleaned: list[str] = []
    for line in lines:
        body = _canon_extpubs(verify_checksum(line))
        kind = classify_btc_key(body)
        _parse_public(body, kind)
        cleaned.append(body)
    return "\n".join(cleaned)


def derive_addresses(raw: str, gap: int | None = None) -> list[dict[str, Any]]:
    """Derive receive (/0) and change (/1) addresses. Never touches private keys."""
    prepared = prepare_watch_descriptor(raw)
    if gap is None:
        gap = gap_limit()
    gap = max(1, min(int(gap), 100))
    lines = prepared.split("\n")
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


def _electrum_request(
    host: str,
    port: int,
    method: str,
    params: list[Any],
    timeout: float = 8.0,
    plain: bool = False,
) -> Any:
    payload = json.dumps({"id": 1, "jsonrpc": "2.0", "method": method, "params": params}) + "\n"
    sock = socket.create_connection((host, port), timeout=timeout)
    try:
        # Electrs on Umbrel is plain TCP on 50001. A saved port 50002 is TLS.
        # Automatic fallbacks stay on plain TCP, including Fulcrum's 50002.
        if not plain and port in {50002, 443, 60002}:
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


def _offline_detail(saved_host: str) -> str:
    hint = local_host_hint(saved_host)
    if hint:
        return (
            f"{hint} Nessun contenitore ha risposto "
            "(electrs_electrs_1, electrs_app_1, fulcrum_fulcrum_1, fulcrum_app_1)."
        )
    return (
        "Electrs non è raggiungibile. Su Umbrel l'host di solito è electrs_electrs_1 "
        "e la porta 50001. Lascia l'host vuoto per cercarlo, oppure scrivilo in Impostazioni."
    )


async def electrs_call(method: str, params: list[Any]) -> Any:
    host, port, _via, plain = await asyncio.to_thread(resolve_electrs)
    if not host:
        saved, _port = _configured_endpoint()
        raise ElectrsError(_offline_detail(saved))
    try:
        return await asyncio.to_thread(
            _electrum_request, host, port, method, params, 8.0, plain,
        )
    except ElectrsError:
        raise
    except Exception:
        _forget_working()
        raise


async def electrs_status() -> dict[str, Any]:
    saved_host, _saved_port = _configured_endpoint()
    host, port, via, _plain = await asyncio.to_thread(resolve_electrs)
    base = {
        "configured": bool(saved_host or host),
        "detected": via not in {"configured", "missing"},
        "connected": False,
        "indexing": False,
        "host": host,
        "port": port,
        "via": "" if via in {"", "missing"} else (f"{host}:{port}" if host else ""),
        "source": "Nodo Bitcoin Umbrel / Electrs (locale)",
    }
    if not host:
        return {**base, "detail": _offline_detail(saved_host)}
    where = f"{host}:{port}"
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
                    "detail": f"Collegato via {where}. Electrs sta ancora indicizzando la blockchain.",
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
                "detail": f"Collegato via {where}. Electrs sta ancora indicizzando.",
            }
        note = ""
        if via != "configured" and saved_host and saved_host != host:
            note = " " + local_host_hint(saved_host) if local_host_hint(saved_host) else (
                f" L'host salvato {saved_host} non risponde da questo contenitore."
            )
        return {
            **base,
            "configured": True,
            "connected": True,
            "height": height,
            "detail": f"Collegato via {where}.{note} Altezza blocco {height}.",
        }
    except Exception as exc:  # noqa: BLE001
        _forget_working()
        return {
            **base,
            "detail": f"Electrs non è raggiungibile su {where}. {exc}",
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
