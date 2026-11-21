"""Interfaccia italiana e API di Casa Viale. Il token non viene mai scritto nei log."""

from __future__ import annotations

import ipaddress
import json
import os
import re
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

import logic

PORT = 8795
STATIC = Path(__file__).resolve().parent / "static"
_UMBREL_DOCKER = (
    ipaddress.ip_network("10.21.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
)


def peer_on_umbrel_docker(ip: str) -> bool:
    """True only for the socket peer, never for a forwarded header."""
    if not ip:
        return False
    try:
        addr = ipaddress.ip_address(ip.split("%", 1)[0].strip("[]"))
    except ValueError:
        return False
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        addr = mapped
    return any(addr in net for net in _UMBREL_DOCKER)

_STROKE = 'fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"'
ICONS = {
    "casa": (
        '<svg viewBox="0 0 24 24" aria-hidden="true">'
        f'<path {_STROKE} d="M4 10.5 12 4l8 6.5V20a1 1 0 0 1-1 1h-5.2v-6.2H10.2V21H5a1 1 0 0 1-1-1z"/>'
        "</svg>"
    ),
    "garage": (
        '<svg viewBox="0 0 24 24" aria-hidden="true">'
        f'<path {_STROKE} d="M14.7 6.3a4.2 4.2 0 0 0-5.8 5.6L4 17l3 3 5.1-5.1a4.2 4.2 0 0 0 5.6-5.6L15.2 11l-2.2-2.2z"/>'
        "</svg>"
    ),
    "documenti": (
        '<svg viewBox="0 0 24 24" aria-hidden="true">'
        f'<rect {_STROKE} x="5" y="3.5" width="14" height="17" rx="2"/>'
        f'<path {_STROKE} d="M8.5 8.5h7M8.5 12h7M8.5 15.5h4.5"/>'
        "</svg>"
    ),
    "immigrazione": (
        '<svg viewBox="0 0 24 24" aria-hidden="true">'
        f'<circle {_STROKE} cx="12" cy="8.5" r="4.6"/>'
        f'<circle {_STROKE} cx="12" cy="8.5" r="1.7"/>'
        f'<path {_STROKE} d="M8 15.2h8M9.2 15.2v3.1M14.8 15.2v3.1M8.4 18.3h7.2"/>'
        "</svg>"
    ),
}


def esc(value) -> str:
    text = "" if value is None else str(value)
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def layout(heading: str, body: str, active: str) -> str:
    links = []
    for sid, name in logic.NAV:
        current = ' aria-current="page"' if sid == active else ""
        links.append(
            f'<a href="/{sid}"{current}>{ICONS[sid]}<span>{esc(name)}</span></a>'
        )
    return f"""<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="format-detection" content="telephone=no">
<title>{esc(heading)}</title>
<link rel="stylesheet" href="/static/style.css">
</head>
<body>
<div class="wrap">
<header><a href="/">Casa Viale</a></header>
{body}
</div>
<nav>{''.join(links)}</nav>
</body>
</html>
"""


def pill(text: str, color: str = "") -> str:
    kind = f" {color}" if color in ("red", "amber", "green") else ""
    return f'<span class="pill{kind}">{esc(text)}</span>'


def tile_head(sid: str, name: str) -> str:
    return (
        f'<span class="tile-top"><span class="mark">{ICONS[sid]}</span>'
        f"<h2>{esc(name)}</h2></span>"
    )


def count_pill(red: int) -> str:
    if red > 0:
        return pill(f"{red} in rosso", "red")
    return pill(f"{red} in rosso")


def page_home() -> str:
    data = logic.home()
    parts = []
    if data["empty"]:
        parts.append(
            '<div class="empty-hero">'
            f'<span class="mark">{ICONS["casa"]}</span>'
            '<p class="empty">Ancora vuoto.</p>'
            "</div>"
        )
    for section in data["sections"]:
        if section["empty"]:
            note = "" if data["empty"] else "<p>Ancora vuoto.</p>"
            inner = tile_head(section["id"], section["name"]) + note
        elif section["next"]:
            nxt = section["next"]
            inner = (
                tile_head(section["id"], section["name"])
                + f'<p class="date">{esc(nxt["due_label"])}</p>'
                + f'<p class="pills">{pill(nxt["distance"], nxt["color"] or "")}{count_pill(section["red"])}</p>'
            )
        else:
            word = section["done_word"] if section["all_done"] else "Da fare"
            tone = "green" if section["all_done"] else ""
            inner = (
                tile_head(section["id"], section["name"])
                + f'<p class="pills">{pill(word, tone)}{count_pill(section["red"])}</p>'
            )
        parts.append(f'<a class="tile" href="/{section["id"]}">{inner}</a>')
    if not data["empty"]:
        parts.append('<h2 class="strip">Prossimi 30 giorni</h2>')
        if not data["upcoming"]:
            parts.append('<p class="quiet">Ancora vuoto.</p>')
        for row in data["upcoming"]:
            when = " · ".join(bit for bit in (row["due_label"], row["distance"]) if bit)
            parts.append(
                f'<a class="row" href="/voce/{esc(row["id"])}">'
                f'<span class="mark">{ICONS[row["section"]]}</span>'
                f'<span class="row-copy"><strong>{esc(row["label"])}</strong>'
                f'<span class="sub">{esc(row["section_name"])}</span>'
                f"{pill(when, row['color'] or '')}</span></a>"
            )
    parts.append('<a class="settings" href="/impostazioni">Impostazioni</a>')
    return layout("Casa Viale", "\n".join(parts), "")


_HTTPS = re.compile(r"https://[^\s<>\"']+")


def first_https(text: str) -> str:
    if not text:
        return ""
    match = _HTTPS.search(text)
    if not match:
        return ""
    return match.group(0).rstrip(".,;:!?)]}")


def note_remainder(text: str) -> str:
    url = first_https(text)
    if not url:
        return text.strip()
    return " ".join(text.replace(url, " ", 1).split())


def _note_raw(payload: dict) -> str:
    return payload.get("note") or payload.get("notes") or ""


def _secondary(view: dict) -> str:
    payload = view["payload"]
    section = view["section"]
    if section == "garage":
        tipo = payload.get("tipo")
        if not tipo:
            return ""
        return logic.GARAGE_TIPI.get(tipo, tipo)
    if section == "documenti":
        return payload.get("persona") or payload.get("person") or ""
    return ""


def page_section(section: str) -> str:
    name = logic.SECTION_NAMES[section]
    items = logic.section_items(section)
    if not items:
        body = (
            '<div class="empty-hero">'
            f'<span class="mark">{ICONS[section]}</span>'
            f"<h1>{esc(name)}</h1>"
            '<p class="empty">Ancora vuoto.</p>'
            "</div>"
        )
        return layout(name, body, section)
    rows = [f"<h1>{esc(name)}</h1>"]
    for view in items:
        extra = ""
        if section == "documenti" and view["payload"].get("numero"):
            extra = f'<span class="sub">Numero {esc(view["payload"]["numero"])}</span>'
        secondary = _secondary(view)
        status = view["distance"] or view["stato_label"]
        bits = [bit for bit in (view["due_label"], secondary) if bit]
        rows.append(
            f'<a class="row" href="/voce/{esc(view["id"])}">'
            f'<span class="mark">{ICONS[section]}</span>'
            f'<span class="row-copy"><strong>{esc(view["label"])}</strong>'
            f'{("<span class=\"sub\">" + esc(" · ".join(bits)) + "</span>") if bits else ""}'
            f"{extra}"
            f'{pill(status, view["color"] or "")}</span></a>'
        )
    return layout(name, "\n".join(rows), section)


def _field_rows(view: dict) -> list[tuple[str, str]]:
    payload = view["payload"]
    section = view["section"]
    rows: list[tuple[str, str]] = []
    if section == "casa":
        rows.append(("Titolo", payload.get("titolo") or payload.get("title") or ""))
        day = payload.get("giorno", payload.get("due_day"))
        if day:
            rows.append(("Giorno del mese", str(day)))
        if payload.get("importo_thb"):
            rows.append(("Importo", f"{payload['importo_thb']} baht"))
        shown = note_remainder(_note_raw(payload))
        if shown:
            rows.append(("Note", shown))
    elif section == "garage":
        rows.append(("Veicolo", payload.get("veicolo") or payload.get("title") or ""))
        if payload.get("tipo") in logic.GARAGE_TIPI:
            rows.append(("Tipo", logic.GARAGE_TIPI[payload["tipo"]]))
        if payload.get("km") is not None:
            rows.append(("Chilometri", str(payload["km"])))
        shown = note_remainder(_note_raw(payload))
        if shown:
            rows.append(("Note", shown))
    elif section == "documenti":
        person = payload.get("persona") or payload.get("person") or ""
        if person:
            rows.append(("Persona", person))
        rows.append(("Tipo di documento", payload.get("tipo") or payload.get("title") or ""))
        if payload.get("numero"):
            rows.append(("Numero", payload["numero"]))
        shown = note_remainder(_note_raw(payload))
        if shown:
            rows.append(("Note", shown))
    else:
        kind = payload.get("tipo")
        rows.append(("Tipo", logic.VISTO_TIPI.get(kind, payload.get("title") or "")))
        if payload.get("ultimo_rapporto"):
            rows.append(("Ultimo rapporto", logic.short_date(payload["ultimo_rapporto"])))
        if payload.get("prossima"):
            rows.append(("Prossima data", logic.short_date(payload["prossima"])))
        elif payload.get("tipo") == "rapporto_90" and payload.get("ultimo_rapporto"):
            computed = date.fromisoformat(payload["ultimo_rapporto"]) + timedelta(days=90)
            rows.append(("Prossimo rapporto", logic.short_date(computed.isoformat())))
        if payload.get("scadenza_visto"):
            rows.append(("Scadenza visto", logic.short_date(payload["scadenza_visto"])))
        shown = note_remainder(_note_raw(payload))
        if shown:
            rows.append(("Note", shown))
    if view["due_label"] and section != "immigrazione":
        rows.append(("Scadenza", view["due_label"]))
    return rows


def page_item(view: dict) -> str:
    rows = "".join(f"<dt>{esc(label)}</dt><dd>{esc(value)}</dd>" for label, value in _field_rows(view))
    color = view["color"] or ""
    status = view["distance"] or view["stato_label"]
    history = ""
    if view["history"]:
        lines = []
        for entry in view["history"]:
            bit = f"{entry['kind_label']} il {entry['paid_label']}" if entry["paid_label"] else entry["kind_label"]
            if entry.get("amount_thb"):
                bit += f" · {entry['amount_thb']} baht"
            if entry.get("note"):
                bit += f" · {entry['note']}"
            lines.append(f"<li>{esc(bit)}</li>")
        history = '<h2>Storico</h2><ul class="history">' + "".join(lines) + "</ul>"
    action = ""
    pay = first_https(_note_raw(view["payload"]))
    if pay:
        action += (
            f'<a class="button" href="{esc(pay)}" target="_blank" rel="noopener noreferrer">Paga</a>'
        )
    if not view["done"]:
        word = "Segna pagato" if view["section"] == "casa" else "Segna fatto"
        action += f'<a class="button" href="/voce/{esc(view["id"])}/conferma">{word}</a>'
    body = (
        '<article class="card">'
        f'<span class="tile-top"><span class="mark">{ICONS[view["section"]]}</span>'
        f"<h1>{esc(view['label'])}</h1></span>"
        f'<p class="pills">{pill(status, color)}</p>'
        f"<dl>{rows}</dl>"
        f"{history}</article>{action}"
    )
    return layout(view["label"], body, view["section"])


def page_confirm(view: dict) -> str:
    if view["done"]:
        return page_item(view)
    question = "Segnare come pagato?" if view["section"] == "casa" else "Segnare come fatto?"
    body = (
        '<article class="card">'
        f'<span class="tile-top"><span class="mark">{ICONS[view["section"]]}</span>'
        f"<h1>{question}</h1></span>"
        f"<p class=\"lead\">{esc(view['label'])}</p></article>"
        f'<form method="post" action="/voce/{esc(view["id"])}/fatto">'
        f'<button type="submit">Conferma</button></form>'
        f'<a class="button secondary" href="/voce/{esc(view["id"])}">Annulla</a>'
    )
    return layout(question, body, view["section"])


def page_settings() -> str:
    text = "Token agente presente" if logic.token_present() else "Token agente assente"
    body = f'<article class="card"><h1>Impostazioni</h1><p class="lead">{text}</p></article>'
    return layout("Impostazioni", body, "")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        try:
            path = urlparse(self.path).path
        except Exception:
            path = ""
        print(f"{self.command} {path}", flush=True)

    def _read_body(self) -> bytes:
        length = self.headers.get("Content-Length", "0")
        try:
            size = int(length)
        except ValueError:
            size = 0
        if size < 0 or size > 1_000_000:
            raise logic.Invalid("body")
        if size == 0:
            return b""
        return self.rfile.read(size)

    def _bearer_state(self) -> str:
        """ok, bad, or absent. The header value is not logged."""
        header = self.headers.get("Authorization", "")
        if not header:
            return "absent"
        scheme, _, rest = header.partition(" ")
        token = rest.strip()
        if scheme.lower() != "bearer" or not token:
            return "absent"
        if logic.token_matches(token):
            return "ok"
        return "bad"

    def _peer_ip(self) -> str:
        if not self.client_address:
            return ""
        return str(self.client_address[0])

    def _write_allowed(self) -> bool:
        state = self._bearer_state()
        if state == "bad":
            return False
        if state == "ok":
            return True
        return peer_on_umbrel_docker(self._peer_ip())

    def _json(self, code: int, payload: dict) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(raw)

    def _html(self, code: int, page: str) -> None:
        raw = page.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(raw)

    def _redirect(self, location: str) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()

    def do_GET(self) -> None:
        try:
            self._get()
        except logic.Invalid:
            self._html(400, layout("Errore", "<p>Dati non validi.</p>", ""))
        except Exception:
            print("errore interno", flush=True)
            self._html(500, layout("Errore", "<p>Errore interno.</p>", ""))

    def _get(self) -> None:
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path == "/api/health":
            self._json(200, {"ok": True})
            return
        if path == "/api/schema":
            self._json(200, logic.IMPORT_EXAMPLE)
            return
        if path == "/api/home":
            self._json(200, logic.home())
            return
        if path == "/api/export":
            self._json(200, logic.export_all())
            return
        if path == "/api/settings":
            self._json(200, {"token_present": logic.token_present()})
            return
        if path.startswith("/api/sections/"):
            section = path.removeprefix("/api/sections/")
            if section not in logic.SECTIONS:
                self._json(404, {"error": "non trovato"})
                return
            self._json(200, {"section": section, "items": [logic.public_item(item) for item in logic.section_items(section)]})
            return
        if path == "/static/style.css":
            raw = (STATIC / "style.css").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/css; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(raw)
            return
        if path == "/favicon.ico":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()
            return
        if path == "/":
            self._html(200, page_home())
            return
        if path == "/impostazioni":
            self._html(200, page_settings())
            return
        if path.strip("/") in logic.SECTIONS:
            self._html(200, page_section(path.strip("/")))
            return
        if path.startswith("/voce/"):
            rest = path.removeprefix("/voce/")
            item_id, _, tail = rest.partition("/")
            view = logic.get_item(item_id)
            if view is None:
                self._html(404, layout("Non trovato", "<p>Non trovato.</p>", ""))
                return
            if tail == "conferma":
                self._html(200, page_confirm(view))
                return
            if tail == "":
                self._html(200, page_item(view))
                return
        self._html(404, layout("Non trovato", "<p>Non trovato.</p>", ""))

    def _invalid(self, exc: logic.Invalid) -> None:
        body = {"error": getattr(exc, "field", None) or "body"}
        external_id = getattr(exc, "external_id", None)
        if external_id:
            body["external_id"] = external_id
        self._json(400, body)

    def do_POST(self) -> None:
        try:
            self._post()
        except logic.Invalid as exc:
            self._invalid(exc)
        except Exception:
            print("errore interno", flush=True)
            self._json(500, {"error": "errore interno"})

    def _json_body(self) -> dict:
        raw = self._read_body()
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise logic.Invalid("body") from exc
        if not isinstance(data, dict):
            raise logic.Invalid("body")
        return data

    def _post(self) -> None:
        path = urlparse(self.path).path.rstrip("/")
        if path == "/api/import":
            if not self._write_allowed():
                self._read_body()
                self._json(401, {"error": "unauthorized"})
                return
            result = logic.import_items(self._json_body())
            self._json(200, result)
            return
        if path == "/api/items":
            item = logic.create_item(self._json_body())
            self._json(201, logic.public_item(item))
            return
        if path.startswith("/api/items/") and path.endswith("/done"):
            if not self._write_allowed():
                self._read_body()
                self._json(401, {"error": "unauthorized"})
                return
            item_id = path.removeprefix("/api/items/").removesuffix("/done").strip("/")
            item = logic.mark_done(item_id, self._json_body())
            if item is None:
                self._json(404, {"error": "non trovato"})
                return
            self._json(200, logic.public_item(item))
            return
        if path.startswith("/voce/") and path.endswith("/fatto"):
            item_id = path.removeprefix("/voce/").removesuffix("/fatto").strip("/")
            self._read_body()
            item = logic.mark_done(item_id, {})
            if item is None:
                self._html(404, layout("Non trovato", "<p>Non trovato.</p>", ""))
                return
            self._redirect(f"/voce/{item_id}")
            return
        self._read_body()
        self._json(404, {"error": "non trovato"})

    def do_DELETE(self) -> None:
        try:
            path = urlparse(self.path).path.rstrip("/")
            prefix = "/api/items/"
            if not path.startswith(prefix):
                self._json(404, {"error": "non trovato"})
                return
            external_id = unquote(path.removeprefix(prefix))
            if not external_id or "/" in external_id:
                self._json(404, {"error": "non trovato"})
                return
            if not self._write_allowed():
                self._json(401, {"error": "unauthorized"})
                return
            if not logic.delete_by_external_id(external_id):
                self._json(404, {"error": "non trovato"})
                return
            self._json(200, {"ok": True})
        except Exception:
            print("errore interno", flush=True)
            self._json(500, {"error": "errore interno"})

    def do_PATCH(self) -> None:
        try:
            path = urlparse(self.path).path.rstrip("/")
            if not path.startswith("/api/items/"):
                self._read_body()
                self._json(404, {"error": "non trovato"})
                return
            if not self._write_allowed():
                self._read_body()
                self._json(401, {"error": "unauthorized"})
                return
            item_id = path.removeprefix("/api/items/")
            if "/" in item_id or not item_id:
                self._read_body()
                self._json(404, {"error": "non trovato"})
                return
            item = logic.patch_item(item_id, self._json_body())
            if item is None:
                self._json(404, {"error": "non trovato"})
                return
            self._json(200, logic.public_item(item))
        except logic.Invalid as exc:
            self._invalid(exc)
        except Exception:
            print("errore interno", flush=True)
            self._json(500, {"error": "errore interno"})


def main() -> None:
    os.umask(0o077)
    logic.ensure_storage()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.daemon_threads = True
    print(f"Casa Viale in ascolto sulla porta {PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
