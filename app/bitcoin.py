"""Watch-only Bitcoin: derive addresses from xpub/zpub/descriptors and query Electrs."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import socket
import ssl
import struct
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
    UMBREL_ELECTRS_PORT,
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


class ConnectTimeout(TimeoutError):
    """The TCP handshake itself did not finish. Do not retry these."""


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
_ATTEMPTS: list[dict[str, Any]] = []
_NETWORK: dict[str, Any] = {}
_PROBE_STEPS: list[dict[str, Any]] = []
_RESOLVE_LOCK = threading.Lock()
_WORKING_TTL = 60.0


MAX_SCAN_INDEX = 1000
WALLET_SCAN_TIMEOUT = 45.0


def branch_cap(last_used: int | None, gap: int) -> int:
    """Hard stop: gap after the last used index, plus 50, never past 1000."""
    base = -1 if last_used is None else int(last_used)
    return max(1, min(MAX_SCAN_INDEX, base + gap + 50))


def gap_limit() -> int:
    raw = db.get_setting("btc_gap_limit") or str(DEFAULT_GAP_LIMIT)
    try:
        value = int(raw)
    except ValueError:
        value = DEFAULT_GAP_LIMIT
    return max(1, min(value, 500))


def consume_until_gap(used_flags: list[bool], gap: int) -> tuple[int, int | None]:
    """How many addresses to read, and the last used index, for one branch.

    Stop after `gap` unused addresses in a row. A used address near the end
    extends the scan instead of cutting it off.
    """
    unused = 0
    seen = 0
    last_used: int | None = None
    for index, used in enumerate(used_flags):
        seen += 1
        if used:
            last_used = index
            unused = 0
        else:
            unused += 1
        if unused >= gap or seen >= MAX_SCAN_INDEX:
            break
    return seen, last_used


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
            "Su Umbrel Electrs è electrs_electrs_1, indirizzo 10.21.21.10, porta 50001, senza SSL. "
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


def _env_endpoint(host_key: str, port_key: str, default_port: int) -> tuple[str, int]:
    host = (os.environ.get(host_key) or "").strip()
    raw = (os.environ.get(port_key) or "").strip()
    try:
        port = int(raw) if raw else default_port
    except ValueError:
        port = default_port
    return host, port


def _candidate_targets() -> list[tuple[str, int, str, bool]]:
    """Saved host, then Umbrel's real Electrs address, then names and the gateway.

    Each item is host, port, via, plain TCP. Fallbacks are plain TCP.
    A saved port 50002 still uses TLS. Umbrel's own Fulcrum port 50002 is plain TCP.
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
    env_host, env_port = _env_endpoint("APP_ELECTRS_NODE_IP", "APP_ELECTRS_NODE_PORT", UMBREL_ELECTRS_PORT)
    if env_host:
        add(env_host, env_port, f"{env_host}:{env_port}", True)
    fulcrum_host, fulcrum_port = _env_endpoint("APP_FULCRUM_NODE_IP", "APP_FULCRUM_NODE_PORT", 50002)
    for candidate, candidate_port in ELECTRS_CANDIDATES:
        add(candidate, candidate_port, f"{candidate}:{candidate_port}", True)
    if fulcrum_host:
        add(fulcrum_host, fulcrum_port, f"{fulcrum_host}:{fulcrum_port}", True)
    domain = (os.environ.get("DEVICE_DOMAIN_NAME") or "").strip()
    if domain:
        add(domain, port or DEFAULT_ELECTRS_PORT, f"{domain}:{port or DEFAULT_ELECTRS_PORT}", True)
        add(domain, 50001, f"{domain}:50001", True)
    gateway_env = (os.environ.get("GATEWAY_IP") or "").strip()
    if gateway_env:
        add(gateway_env, 50001, f"{gateway_env}:50001", True)
        add(gateway_env, 50002, f"{gateway_env}:50002", True)
    for gateway in _gateway_hosts():
        add(gateway, port or DEFAULT_ELECTRS_PORT, f"{gateway}:{port or DEFAULT_ELECTRS_PORT}", True)
        add(gateway, 50001, f"{gateway}:50001", True)
        add(gateway, 50002, f"{gateway}:50002", True)
    return out


def _failure_text(exc: BaseException) -> str:
    if isinstance(exc, socket.gaierror):
        return f"DNS: {exc}"
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return f"Timeout: {exc}"
    if isinstance(exc, ConnectionRefusedError):
        return f"Connessione rifiutata: {exc}"
    if isinstance(exc, (ElectrsError, json.JSONDecodeError)):
        return f"Errore di protocollo: {exc}"
    if isinstance(exc, OSError) and exc.errno in {101, 113}:
        return f"Rete non raggiungibile: {exc}"
    return f"Errore: {exc}"


def _interface_report() -> list[str]:
    """IPv4 addresses of this container, as 'interfaccia indirizzo'."""
    found: list[str] = []
    try:
        names = os.listdir("/sys/class/net")
    except OSError:
        return found
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        for name in sorted(names):
            try:
                packed = fcntl_ioctl(sock.fileno(), name)
            except OSError:
                continue
            ip = socket.inet_ntoa(packed[20:24])
            found.append(f"{name} {ip}")
    finally:
        sock.close()
    return found


def fcntl_ioctl(fileno: int, name: str) -> bytes:
    import fcntl

    return fcntl.ioctl(fileno, 0x8915, struct.pack("256s", name.encode()[:15]))


def network_report() -> dict[str, Any]:
    """What this container can see: its addresses, interfaces, and default gateway."""
    gateways = _gateway_hosts()
    env_gateway = (os.environ.get("GATEWAY_IP") or "").strip()
    if env_gateway and env_gateway not in gateways:
        gateways = [env_gateway, *gateways]
    try:
        interfaces = sorted(os.listdir("/sys/class/net"))
    except OSError:
        interfaces = []
    return {
        "ips": _interface_report(),
        "networks": interfaces,
        "gateway": gateways[0] if gateways else "",
        "gateways": gateways,
        "domain": (os.environ.get("DEVICE_DOMAIN_NAME") or "").strip(),
        "electrs_ip": (os.environ.get("APP_ELECTRS_NODE_IP") or "").strip(),
        "electrs_port": (os.environ.get("APP_ELECTRS_NODE_PORT") or "").strip(),
    }


def _forget_working() -> None:
    global _WORKING, _WORKING_AT, _WORKING_KEY
    _WORKING = None
    _WORKING_AT = 0.0
    _WORKING_KEY = ""


def resolve_electrs() -> tuple[str, int, str, bool]:
    """First Electrum server that answers server.version. Cached for a minute."""
    global _WORKING, _WORKING_AT, _WORKING_KEY, _ATTEMPTS, _NETWORK, _PROBE_STEPS
    configured_host, configured_port = _configured_endpoint()
    key = f"{configured_host}:{configured_port}"
    with _RESOLVE_LOCK:
        now = time.time()
        if _WORKING_KEY == key and now - _WORKING_AT < _WORKING_TTL:
            return _WORKING or ("", configured_port, "missing", True)
        attempts: list[dict[str, Any]] = []
        found: tuple[str, int, str, bool] | None = None
        for host, port, via, plain in _candidate_targets():
            if not _host_resolves(host):
                attempts.append({
                    "host": host,
                    "port": port,
                    "ok": False,
                    "error": "DNS: nome non risolto",
                })
                continue
            started = time.perf_counter()
            try:
                version = _electrum_request(
                    host,
                    port,
                    "server.version",
                    [ELECTRUM_CLIENT, ELECTRUM_PROTOCOL],
                    timeout=VERSION_TIMEOUT,
                    plain=plain,
                )
            except Exception as exc:
                attempts.append({
                    "host": host,
                    "port": port,
                    "ok": False,
                    "ms": _ms(started),
                    "error": _failure_text(exc),
                })
                log.info("Electrum %s:%s non risponde: %s", host, port, attempts[-1]["error"])
                continue
            version_ms = _ms(started)
            connect_ms = _LAST_CONNECT_MS
            _PROBE_STEPS = [
                {
                    "name": "TCP connect",
                    "ok": True,
                    "ms": connect_ms if connect_ms is not None else version_ms,
                    "detail": f"{host}:{port}",
                },
                {
                    "name": "server.version",
                    "ok": True,
                    "ms": version_ms,
                    "detail": _banner(version) or ELECTRUM_PROTOCOL,
                },
            ]
            attempts.append({"host": host, "port": port, "ok": True, "ms": version_ms, "error": ""})
            found = (host, port, via, plain)
            break
        _ATTEMPTS = attempts
        _NETWORK = network_report()
        _WORKING = found
        _WORKING_KEY = key
        _WORKING_AT = now
        if found:
            return found
        return "", configured_port, "missing", True


def electrs_diagnostics() -> dict[str, Any]:
    return {"attempts": list(_ATTEMPTS), "network": dict(_NETWORK), "steps": list(_PROBE_STEPS)}


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


def _norm_desc(body: str) -> str:
    return re.sub(r"\s+", "", (body or "").split("#")[0])


def _is_multipath(body: str) -> bool:
    return "<0;1>" in body or "{0,1}" in body


def _retarget_branch(body: str, src: str, dst: str) -> str:
    """Swap only the wildcard step, not an origin like /0h/ or /0'/."""
    return re.sub(rf"/{src}/\*", f"/{dst}/*", body)


def branch_plans(raw: str) -> list[tuple[str, str, int]]:
    """Receive and change for every descriptor, even when only one branch was pasted.

    Each item is (role, descriptor body, embit branch index).
    """
    prepared = prepare_watch_descriptor(raw)
    plans: list[tuple[str, str, int]] = []
    seen: set[tuple[str, str, int]] = set()

    def add(role: str, body: str, branch: int) -> None:
        key = (role, _norm_desc(body), branch)
        if key in seen:
            return
        seen.add(key)
        plans.append((role, body, branch))

    for line in prepared.split("\n"):
        body = line.split("#")[0].strip()
        if not body:
            continue
        kind = classify_btc_key(body)
        if kind not in {"descriptor", "multisig"}:
            add("receive", body, 0)
            add("change", body, 1)
            continue
        if _is_multipath(body):
            add("receive", body, 0)
            add("change", body, 1)
            continue
        if re.search(r"/0/\*", body):
            add("receive", body, 0)
            add("change", _retarget_branch(body, "0", "1"), 0)
            continue
        if re.search(r"/1/\*", body):
            add("receive", _retarget_branch(body, "1", "0"), 0)
            add("change", body, 0)
            continue
        desc = _parse_public(body, kind)
        if (getattr(desc, "num_branches", 1) or 1) >= 2:
            add("receive", body, 0)
            add("change", body, 1)
        else:
            add("receive", body, 0)
    return plans


def _derive_at(body: str, branch: int, index: int):
    kind = classify_btc_key(body)
    desc = _parse_public(body, kind)
    try:
        return desc.derive(index, branch)
    except TypeError:
        return desc.derive(index)


def derive_addresses(raw: str, gap: int | None = None) -> list[dict[str, Any]]:
    """Derive receive (/0) and change (/1) addresses. Never touches private keys."""
    if gap is None:
        gap = gap_limit()
    gap = max(1, min(int(gap), 500))
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for role, body, branch in branch_plans(raw):
        for index in range(gap):
            derived = _derive_at(body, branch, index)
            addr = derived.address(NETWORKS["main"])
            if addr in seen:
                continue
            seen.add(addr)
            out.append(
                {
                    "address": addr,
                    "scriptpubkey": derived.script_pubkey().data.hex(),
                    "branch": 1 if role == "change" else 0,
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


CONNECT_TIMEOUT = 5.0
VERSION_TIMEOUT = 60.0
BATCH_TIMEOUT = 120.0
BATCH_SIZE = 20
SCRIPT_CACHE_TTL = 10 * 60
ELECTRUM_CLIENT = "nodo-defi-monitor"
ELECTRUM_PROTOCOL = "1.4"
_LAST_CONNECT_MS: int | None = None
_PROBE_STEPS: list[dict[str, Any]] = []
_SCRIPT_CACHE: dict[tuple[str, str], tuple[float, Any]] = {}
_PROGRESS: dict[int, dict[str, Any]] = {}
_SCAN_TASK: asyncio.Task | None = None
_NODE: dict[str, Any] | None = None
_ON_BTC_DONE = None


def _format_request(req_id: int, method: str, params: list[Any]) -> bytes:
    """Electrum 1.4 / Electrs 0.11: one JSON object, then a newline."""
    payload = {"id": req_id, "method": method, "params": params}
    return (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")


def _request_timeout(method: str) -> float:
    if method.startswith("blockchain.scripthash.") or method == "blockchain.transaction.get":
        return BATCH_TIMEOUT
    return VERSION_TIMEOUT


def _ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _banner(result: Any) -> str:
    if isinstance(result, list) and result:
        parts = [str(part) for part in result[:2]]
        return " / ".join(parts)
    if isinstance(result, str):
        return result
    return ""


def _open_socket(host: str, port: int, plain: bool) -> tuple[socket.socket, int]:
    global _LAST_CONNECT_MS
    started = time.perf_counter()
    try:
        sock = socket.create_connection((host, port), timeout=CONNECT_TIMEOUT)
    except (TimeoutError, socket.timeout) as exc:
        raise ConnectTimeout(str(exc)) from exc
    _LAST_CONNECT_MS = _ms(started)
    if not plain and port in {50002, 443, 60002}:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        sock = ctx.wrap_socket(sock, server_hostname=host)
    return sock, _LAST_CONNECT_MS


def _read_messages(sock: socket.socket, timeout: float, want: int) -> list[dict[str, Any]]:
    sock.settimeout(timeout)
    buf = b""
    found: list[dict[str, Any]] = []
    while len(found) < want:
        if b"\n" not in buf:
            data = sock.recv(65536)
            if not data:
                break
            buf += data
            continue
        line, buf = buf.split(b"\n", 1)
        raw = line.decode("utf-8").strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ElectrsError(f"Errore di protocollo: {exc}") from exc
        if not isinstance(msg, dict):
            raise ElectrsError("Errore di protocollo: risposta non è un oggetto")
        if msg.get("error"):
            raise ElectrsError(str(msg["error"]))
        found.append(msg)
    if len(found) < want:
        raise ElectrsError("Risposta vuota da Electrs")
    return found


def _electrum_exchange(
    host: str,
    port: int,
    calls: list[tuple[str, list[Any]]],
    timeout: float,
    plain: bool,
) -> list[Any]:
    """Send newline-terminated calls on one socket and collect the replies."""
    sock, _connect_ms = _open_socket(host, port, plain)
    try:
        frames = [_format_request(index, method, params) for index, (method, params) in enumerate(calls, start=1)]
        sock.sendall(b"".join(frames))
        messages = _read_messages(sock, timeout, len(calls))
        results: dict[int, Any] = {}
        for msg in messages:
            req_id = msg.get("id")
            if isinstance(req_id, int):
                results[req_id] = msg.get("result")
        return [results.get(index) for index in range(1, len(calls) + 1)]
    finally:
        try:
            sock.close()
        except Exception:
            pass


def _electrum_request(
    host: str,
    port: int,
    method: str,
    params: list[Any],
    timeout: float = VERSION_TIMEOUT,
    plain: bool = False,
) -> Any:
    """One Electrum call. A timeout is tried a second time."""
    last: BaseException | None = None
    for _attempt in range(2):
        try:
            return _electrum_exchange(host, port, [(method, params)], timeout, plain)[0]
        except ConnectTimeout:
            raise
        except (TimeoutError, socket.timeout) as exc:
            last = exc
            continue
    assert last is not None
    raise last


def _offline_detail(saved_host: str) -> str:
    hint = local_host_hint(saved_host)
    if hint:
        return (
            f"{hint} Nessun indirizzo ha risposto, nemmeno 10.21.21.10:50001 "
            "(electrs_electrs_1). Apri la diagnostica qui sotto."
        )
    return (
        "Electrs non è raggiungibile. Su Umbrel il server è 10.21.21.10:50001 "
        "(contenitore electrs_electrs_1, TCP senza SSL). Apri la diagnostica qui sotto."
    )


async def electrs_call(method: str, params: list[Any], timeout: float | None = None) -> Any:
    host, port, _via, plain = await asyncio.to_thread(resolve_electrs)
    if not host:
        saved, _port = _configured_endpoint()
        raise ElectrsError(_offline_detail(saved))
    try:
        return await asyncio.to_thread(
            _electrum_request,
            host,
            port,
            method,
            params,
            timeout if timeout is not None else _request_timeout(method),
            plain,
        )
    except ElectrsError:
        raise
    except (TimeoutError, socket.timeout):
        raise
    except OSError as exc:
        if isinstance(exc, ConnectionRefusedError) or exc.errno in {101, 111, 113}:
            _forget_working()
        raise


_MISS = object()


def _script_cache_get(method: str, scripthash: str) -> Any:
    hit = _SCRIPT_CACHE.get((method, scripthash))
    if not hit:
        return _MISS
    stored, value = hit
    if time.time() - stored > SCRIPT_CACHE_TTL:
        return _MISS
    return value


def _script_cache_put(method: str, scripthash: str, value: Any) -> None:
    _SCRIPT_CACHE[(method, scripthash)] = (time.time(), value)


async def query_scripthashes(method: str, scripthashes: list[str]) -> dict[str, Any]:
    """Read many scripthashes in batches of 20. A repeat within 10 minutes is cached."""
    found: dict[str, Any] = {}
    missing: list[str] = []
    for scripthash in scripthashes:
        cached = _script_cache_get(method, scripthash)
        if cached is _MISS:
            missing.append(scripthash)
        else:
            found[scripthash] = cached
    if not missing:
        return found
    host, port, _via, plain = await asyncio.to_thread(resolve_electrs)
    if not host:
        saved, _port = _configured_endpoint()
        raise ElectrsError(_offline_detail(saved))
    for start in range(0, len(missing), BATCH_SIZE):
        batch = missing[start:start + BATCH_SIZE]
        calls = [(method, [scripthash]) for scripthash in batch]
        rows = await asyncio.to_thread(_electrum_request_many, host, port, calls, BATCH_TIMEOUT, plain)
        for scripthash, value in zip(batch, rows):
            _script_cache_put(method, scripthash, value)
            found[scripthash] = value
    return found


def _electrum_request_many(
    host: str,
    port: int,
    calls: list[tuple[str, list[Any]]],
    timeout: float,
    plain: bool,
) -> list[Any]:
    last: BaseException | None = None
    for _attempt in range(2):
        try:
            return _electrum_exchange(host, port, calls, timeout, plain)
        except ConnectTimeout:
            raise
        except (TimeoutError, socket.timeout) as exc:
            last = exc
            continue
    assert last is not None
    raise last


async def electrs_status() -> dict[str, Any]:
    global _PROBE_STEPS
    saved_host, _saved_port = _configured_endpoint()
    host, port, via, _plain = await asyncio.to_thread(resolve_electrs)
    steps = list(_PROBE_STEPS)
    where = f"{host}:{port}" if host else ""

    async def _step(name: str, method: str, params: list[Any]) -> Any:
        started = time.perf_counter()
        try:
            result = await electrs_call(method, params)
        except Exception as exc:  # noqa: BLE001
            steps.append({"name": name, "ok": False, "ms": _ms(started), "detail": _failure_text(exc)})
            return None
        detail = ""
        if name == "server.features" and isinstance(result, dict):
            detail = "ok"
        elif name == "blockchain.headers.subscribe" and isinstance(result, dict) and result.get("height") is not None:
            detail = f"altezza {result.get('height')}"
        elif name.startswith("scripthash"):
            detail = "ok" if result is not None else ""
        steps.append({"name": name, "ok": True, "ms": _ms(started), "detail": detail})
        return result

    height = None
    indexing = False
    if host:
        await _step("server.features", "server.features", [])
        headers = await _step("blockchain.headers.subscribe", "blockchain.headers.subscribe", [])
        if isinstance(headers, dict):
            height = headers.get("height")
        await _step(
            "scripthash.get_balance",
            "blockchain.scripthash.get_balance",
            ["00" * 32],
        )
        if height == 0 or any(
            not step.get("ok") and _INDEX_RE.search(str(step.get("detail") or ""))
            for step in steps
        ):
            indexing = True
    diag_steps = steps
    _PROBE_STEPS = diag_steps
    diag = electrs_diagnostics()
    base = {
        "configured": bool(saved_host or host),
        "detected": via not in {"configured", "missing"},
        "connected": bool(host),
        "indexing": indexing,
        "host": host,
        "port": port,
        "via": where,
        "source": "Nodo Bitcoin Umbrel / Electrs (locale)",
        "attempts": diag["attempts"],
        "network": diag["network"],
        "steps": diag["steps"],
        "height": height,
    }
    if not host:
        return {**base, "detail": _offline_detail(saved_host)}
    failed = [step for step in diag_steps if not step.get("ok")]
    note = ""
    if via != "configured" and saved_host and saved_host != host:
        note = " " + local_host_hint(saved_host) if local_host_hint(saved_host) else (
            f" L'host salvato {saved_host} non risponde da questo contenitore."
        )
    if indexing:
        detail = f"Collegato via {where}.{note} Electrs sta ancora indicizzando."
    elif failed:
        first = failed[0]
        detail = (
            f"Collegato via {where}.{note} {first['name']} non è riuscito "
            f"({first.get('detail') or 'errore'}, {first.get('ms')} ms)."
        )
    else:
        detail = f"Collegato via {where}.{note} Altezza blocco {height}."
    global _NODE
    _NODE = dict(base)
    return {**base, "configured": True, "detail": detail}


def _utxo_key(utxo: dict[str, Any]) -> tuple[str, int] | None:
    txid = utxo.get("tx_hash") or utxo.get("txid")
    pos = utxo.get("tx_pos")
    if pos is None:
        pos = utxo.get("vout")
    if not txid or pos is None:
        return None
    return str(txid), int(pos)


def _utxo_sats(utxo: dict[str, Any]) -> int:
    value = utxo.get("value")
    if value is None:
        return 0
    return int(value)


def _utxo_unconfirmed(utxo: dict[str, Any]) -> bool:
    height = utxo.get("height")
    return height in (None, 0) or (isinstance(height, int) and height <= 0)


def _progress_set(wallet_id: int, **fields: Any) -> None:
    row = _PROGRESS.setdefault(int(wallet_id), {"id": int(wallet_id)})
    row.update(fields)


def progress_view() -> dict[str, Any]:
    running = _SCAN_TASK is not None and not _SCAN_TASK.done()
    return {"running": running, "wallets": [dict(row) for row in _PROGRESS.values()]}


def _scan_cache_key(wallet_id: int) -> str:
    return f"btc-scan:{int(wallet_id)}"


def load_scan_cache(wallet_id: int) -> dict[str, Any]:
    stored = db.cache_get(_scan_cache_key(wallet_id)) or {}
    return stored if isinstance(stored, dict) else {}


def save_scan_cache(wallet_id: int, payload: dict[str, Any]) -> None:
    db.cache_set(_scan_cache_key(wallet_id), payload)


def cached_node(has_wallets: bool) -> dict[str, Any]:
    if isinstance(_NODE, dict):
        return dict(_NODE)
    return {
        "configured": has_wallets,
        "connected": False,
        "indexing": False,
        "host": "",
        "port": 0,
        "detail": "Controllo Electrs in corso, in background.",
        "source": "Nodo Bitcoin Umbrel / Electrs (locale)",
        "attempts": [],
        "steps": [],
        "network": {},
    }


def preview_wallet(wallet: dict[str, Any]) -> dict[str, Any]:
    """Last saved balance, or an empty card, without waiting for a scan."""
    cached = load_scan_cache(int(wallet["id"]))
    result = cached.get("result") if isinstance(cached.get("result"), dict) else None
    if result:
        view = dict(result)
        view["scanning"] = True
        view["stale"] = True
        view.setdefault("scanned_at", cached.get("cached_at") or cached.get("ts"))
        return view
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
        "status": "In scansione",
        "last": "Scansione in background.",
        "connected": False,
        "scanning": True,
        "stale": False,
        "addresses": [],
        "txs": [],
        "reuse": [],
        "dust": [],
        "scan": {
            "gap": gap_limit(),
            "receive": {"scanned": 0, "last_used": None},
            "change": {"scanned": 0, "last_used": None},
            "utxo_count": 0,
            "confirmed_sats": 0,
            "unconfirmed_sats": 0,
        },
        "alert_out_btc": wallet.get("alert_out_btc"),
        "alert_min_btc": wallet.get("alert_min_btc"),
        "hot_threshold_btc": wallet.get("hot_threshold_btc"),
    }


async def _fill_chunk(chunk: list[dict[str, Any]], scripts: dict[str, Any]) -> None:
    """Reuse a scripthash when its status hash is unchanged. Otherwise read history and UTXOs."""
    hashes = [_scripthash(item["scriptpubkey"]) for item in chunk]
    try:
        statuses = await query_scripthashes("blockchain.scripthash.subscribe", hashes)
    except Exception:  # noqa: BLE001
        statuses = {}
    missing: list[str] = []
    for item, scripthash in zip(chunk, hashes):
        prev = scripts.get(scripthash) if isinstance(scripts.get(scripthash), dict) else {}
        status = statuses.get(scripthash)
        if prev and status and prev.get("status") == status:
            item["history"] = list(prev.get("history") or [])
            item["utxos"] = list(prev.get("utxos") or [])
            continue
        missing.append(scripthash)
    histories: dict[str, Any] = {}
    unspents: dict[str, Any] = {}
    if missing:
        histories = await query_scripthashes("blockchain.scripthash.get_history", missing)
        unspents = await query_scripthashes("blockchain.scripthash.listunspent", missing)
    for item, scripthash in zip(chunk, hashes):
        if scripthash not in missing:
            continue
        hist = list(histories.get(scripthash) or [])
        utxos = list(unspents.get(scripthash) or [])
        if len(hist) > 20:
            hist = hist[:20]
        item["history"] = hist
        item["utxos"] = utxos
        scripts[scripthash] = {"status": statuses.get(scripthash), "history": hist, "utxos": utxos}


async def _scan_branch(
    role: str,
    body: str,
    branch: int,
    gap: int,
    known_last: int | None,
    scripts: dict[str, Any],
    deadline: float,
    wallet_id: int,
) -> dict[str, Any]:
    """Stop after `gap` unused addresses, or at last_used + gap + 50, and never past 1000."""
    rows: list[dict[str, Any]] = []
    index = 0
    unused = 0
    last_used: int | None = known_last if isinstance(known_last, int) else None
    cap = branch_cap(last_used, gap)
    while unused < gap and index < cap:
        if time.monotonic() > deadline:
            raise TimeoutError("Scansione Bitcoin interrotta: tempo scaduto")
        width = min(BATCH_SIZE, cap - index)
        if width <= 0:
            break
        chunk = []
        for offset in range(width):
            derived = _derive_at(body, branch, index + offset)
            chunk.append(
                {
                    "address": derived.address(NETWORKS["main"]),
                    "scriptpubkey": derived.script_pubkey().data.hex(),
                    "branch": 1 if role == "change" else 0,
                    "index": index + offset,
                    "role": role,
                }
            )
        await _fill_chunk(chunk, scripts)
        for item in chunk:
            hist = list(item.get("history") or [])
            utxos = list(item.get("utxos") or [])
            used = bool(hist) or bool(utxos)
            rows.append(item)
            index += 1
            _progress_set(
                wallet_id,
                branch=role,
                index=item["index"],
                checked=index,
                status="In scansione",
            )
            if used:
                last_used = item["index"]
                unused = 0
                cap = branch_cap(last_used, gap)
            else:
                unused += 1
            if unused >= gap or index >= cap:
                break
    return {"role": role, "scanned": len(rows), "last_used": last_used, "rows": rows}


async def scan_wallet(wallet: dict[str, Any], gap: int | None = None) -> dict[str, Any]:
    if gap is None:
        gap = gap_limit()
    gap = max(1, min(int(gap), 500))
    wallet_id = int(wallet["id"])
    cached = load_scan_cache(wallet_id)
    scripts = dict(cached.get("scripts") or {})
    known_branches = cached.get("branches") if isinstance(cached.get("branches"), dict) else {}
    deadline = time.monotonic() + WALLET_SCAN_TIMEOUT
    try:
        plans = branch_plans(wallet["descriptor"])
    except Exception as exc:
        log.warning("Derivazione Bitcoin non riuscita (%s).", type(exc).__name__)
        raise
    _progress_set(wallet_id, label=wallet.get("label"), owner=wallet.get("owner"), status="In scansione", branch="", index=0, checked=0)
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
    reuse: list[str] = []
    dust: list[dict[str, Any]] = []
    branches: dict[str, dict[str, Any]] = {}
    seen_outpoints: set[tuple[str, int]] = set()
    confirmed_sats = 0
    unconfirmed_sats = 0
    try:
        for role, body, branch in plans:
            known_last = (known_branches.get(role) or {}).get("last_used") if isinstance(known_branches.get(role), dict) else None
            report = await _scan_branch(role, body, branch, gap, known_last, scripts, deadline, wallet_id)
            previous = branches.get(role)
            if previous is None:
                branches[role] = {"scanned": report["scanned"], "last_used": report["last_used"]}
            else:
                previous["scanned"] += report["scanned"]
                if report["last_used"] is not None and (
                    previous["last_used"] is None or report["last_used"] > previous["last_used"]
                ):
                    previous["last_used"] = report["last_used"]
            for item in report["rows"]:
                history = item["history"]
                if not history and not item["utxos"]:
                    continue
                if len(history) > 1 and item["role"] == "receive":
                    reuse.append(item["address"])
                address_sats = 0
                for utxo in item["utxos"]:
                    if not isinstance(utxo, dict):
                        continue
                    key = _utxo_key(utxo)
                    if key is None or key in seen_outpoints:
                        continue
                    seen_outpoints.add(key)
                    value = _utxo_sats(utxo)
                    address_sats += value
                    if _utxo_unconfirmed(utxo):
                        unconfirmed_sats += value
                    else:
                        confirmed_sats += value
                if address_sats == 0 and not history:
                    continue
                kept = {key: value for key, value in item.items() if key not in {"history", "utxos"}}
                used.append({**kept, "sats": address_sats, "tx_count": len(history)})
                for ev in history:
                    txs.append(
                        {
                            "txid": ev.get("tx_hash"),
                            "height": ev.get("height") or 0,
                            "address": item["address"],
                            "role": item["role"],
                        }
                    )
                if address_sats and 0 < address_sats <= 1000:
                    dust.append({"address": item["address"], "sats": address_sats})
    except Exception as exc:  # noqa: BLE001
        if _INDEX_RE.search(str(exc)):
            status = {**status, "indexing": True, "connected": False, "detail": str(exc)}
        else:
            log.warning("Lettura Electrs non riuscita (%s).", type(exc).__name__)
    total = confirmed_sats + unconfirmed_sats
    utxo_count = len(seen_outpoints)
    scan = {
        "gap": gap,
        "receive": branches.get("receive") or {"scanned": 0, "last_used": None},
        "change": branches.get("change") or {"scanned": 0, "last_used": None},
        "utxo_count": utxo_count,
        "confirmed_sats": confirmed_sats,
        "unconfirmed_sats": unconfirmed_sats,
    }

    # Fetch a few recent transactions to classify incoming/outgoing roughly via status list
    txs.sort(key=lambda t: t.get("height") or 0, reverse=True)
    recent: list[dict[str, Any]] = []
    seen_tx: set[str] = set()
    ordered: list[dict[str, Any]] = []
    for ev in txs:
        txid = ev.get("txid")
        if not txid or txid in seen_tx:
            continue
        seen_tx.add(txid)
        ordered.append(ev)
        if len(ordered) >= 12:
            break
    raw_by_id: dict[str, Any] = {}
    if ordered:
        try:
            tx_host, tx_port, _tx_via, tx_plain = await asyncio.to_thread(resolve_electrs)
            fetched = await asyncio.to_thread(
                _electrum_request_many,
                tx_host,
                tx_port,
                [("blockchain.transaction.get", [ev["txid"], True]) for ev in ordered],
                BATCH_TIMEOUT,
                tx_plain,
            ) if tx_host else []
            raw_by_id = {ev["txid"]: value for ev, value in zip(ordered, fetched)}
        except Exception:
            raw_by_id = {}
    for ev in ordered:
        txid = ev.get("txid")
        raw_tx = raw_by_id.get(txid)
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

    result = {
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
        "scan": scan,
        "scanning": False,
        "stale": False,
        "scanned_at": int(time.time()),
        "node": status,
    }
    stored = {key: value for key, value in result.items() if key != "node"}
    save_scan_cache(wallet_id, {"result": stored, "scripts": scripts, "branches": branches, "ts": stored["scanned_at"]})
    _progress_set(wallet_id, status="Sincronizzato", branch="", checked=scan["receive"]["scanned"] + scan["change"]["scanned"], btc=stored["btc"], stale=False, ts=stored["scanned_at"])
    return result


def set_scan_callback(callback) -> None:
    global _ON_BTC_DONE
    _ON_BTC_DONE = callback


def _keep_previous(previous: dict[str, Any], status: str, last: str) -> dict[str, Any]:
    kept = dict(previous)
    kept["scanning"] = False
    kept["stale"] = bool(previous.get("scanned_at") or previous.get("btc"))
    kept["status"] = status
    kept["last"] = last if kept["stale"] else previous.get("last") or last
    return kept


async def _scan_all(wallets: list[dict[str, Any]]) -> None:
    current = [preview_wallet(w) for w in wallets]
    node = cached_node(True)
    try:
        node = await asyncio.wait_for(electrs_status(), timeout=8)
    except Exception as exc:  # noqa: BLE001
        node = {**cached_node(True), "detail": f"Electrs non ricontrollato: {exc}", "connected": False}
    for index, wallet in enumerate(wallets):
        wallet_id = int(wallet["id"])
        previous = current[index]
        try:
            result = await asyncio.wait_for(scan_wallet(wallet), timeout=WALLET_SCAN_TIMEOUT)
            if not result.get("connected") and previous.get("stale"):
                result = _keep_previous(previous, "Ultimo saldo", "Electrs non raggiungibile. Ultimo saldo conservato.")
        except Exception as exc:  # noqa: BLE001
            timed_out = isinstance(exc, (TimeoutError, asyncio.TimeoutError))
            result = _keep_previous(
                previous,
                "Tempo scaduto" if timed_out else "Errore",
                "Ultimo saldo conservato.",
            )
            _progress_set(
                wallet_id,
                label=wallet.get("label"),
                owner=wallet.get("owner"),
                status=result["status"],
                stale=result["stale"],
                btc=result.get("btc") or 0,
                ts=result.get("scanned_at"),
            )
        current[index] = result
        if _ON_BTC_DONE:
            try:
                _ON_BTC_DONE(list(current), node)
            except Exception:  # noqa: BLE001
                log.warning("Aggiornamento saldo Bitcoin non applicato")


def schedule_scans(wallets: list[dict[str, Any]]) -> None:
    """Start the Bitcoin scan without waiting for it. A scan already running is left alone."""
    global _SCAN_TASK
    if not wallets:
        return
    if _SCAN_TASK is not None and not _SCAN_TASK.done():
        return
    _SCAN_TASK = asyncio.create_task(_scan_all(wallets))
