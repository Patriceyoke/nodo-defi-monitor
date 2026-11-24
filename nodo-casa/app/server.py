"""Interfaccia italiana e API di Casa Viale. Il token non viene mai scritto nei log."""

from __future__ import annotations

import ipaddress
import json
import os
import re
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

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
    "carte": (
        '<svg viewBox="0 0 24 24" aria-hidden="true">'
        f'<rect {_STROKE} x="3" y="6" width="18" height="12" rx="2"/>'
        f'<path {_STROKE} d="M3 10h18"/>'
        "</svg>"
    ),
    "spese": (
        '<svg viewBox="0 0 24 24" aria-hidden="true">'
        f'<path {_STROKE} d="M7 3.5h10v17l-1.7-1.1L13.6 20.5 12 19.4 10.4 20.5 8.7 19.4 7 20.5z"/>'
        f'<path {_STROKE} d="M9.2 8h5.6M9.2 11.5h5.6M9.2 15h3.4"/>'
        "</svg>"
    ),
}
CHART_COLORS = {
    "bollette": "#1e6b45",
    "condominio": "#1d4e89",
    "abbonamenti": "#6b3fa0",
    "garage": "#0e6b6b",
    "documenti": "#8a5a12",
    "altro": "#5c5346",
    "carte": "#7a2e0e",
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


def _rincaro_badge(view: dict) -> str:
    alert = view.get("rincaro") or {}
    if not alert:
        return ""
    return f'<span class="badge rincaro">{esc(alert.get("badge") or "")}</span>'


def donut_svg(slices: list[dict]) -> str:
    if not slices:
        return ""
    radius = 54
    circ = 2 * 3.14159265 * radius
    offset = 0.0
    total = sum(float(item["amount_thb"]) for item in slices) or 1
    circles = []
    for item in slices:
        length = float(item["amount_thb"]) / total * circ
        color = CHART_COLORS.get(item["id"], "#1e6b45")
        circles.append(
            f'<circle cx="72" cy="72" r="{radius}" fill="none" stroke="{color}" stroke-width="24" '
            f'stroke-dasharray="{length:.2f} {circ - length:.2f}" stroke-dashoffset="{-offset:.2f}" '
            'transform="rotate(-90 72 72)"/>'
        )
        offset += length
    return f'<svg class="donut" viewBox="0 0 144 144" role="img">{"".join(circles)}</svg>'


def bar_rows(slices: list[dict]) -> str:
    if not slices:
        return ""
    peak = max(float(item["amount_thb"]) for item in slices) or 1
    rows = []
    for item in slices:
        width = max(4, float(item["amount_thb"]) / peak * 100)
        color = CHART_COLORS.get(item["id"], "#1e6b45")
        rows.append(
            "<div class=\"bar-row\">"
            f"<span>{esc(item['label'])}</span>"
            f"<span class=\"bar\"><span style=\"width:{width:.1f}%;background:{color}\"></span></span>"
            f"<strong>{esc(logic.format_baht(item['amount_thb']))}</strong>"
            "</div>"
        )
    return "".join(rows)


def page_home(periodo: str = "mese", with_cards: bool = False) -> str:
    data = logic.home()
    parts = []
    for alert in data.get("rincari") or []:
        parts.append(f'<p class="banner rincaro">{esc(alert["text"])}</p>')
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
            date_bit = row["due_label"]
            if row.get("amount_label"):
                date_bit = f"{date_bit} · {row['amount_label']}"
            when = " · ".join(bit for bit in (date_bit, row["distance"]) if bit)
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
    def row_html(view: dict, force_green: bool = False) -> str:
        extra = ""
        if section == "documenti" and view["payload"].get("numero"):
            extra = f'<span class="sub">Numero {esc(view["payload"]["numero"])}</span>'
        secondary = _secondary(view)
        date_bit = view["due_label"]
        if view.get("amount_label"):
            date_bit = f"{date_bit} · {view['amount_label']}"
        bits = [bit for bit in (date_bit, secondary) if bit]
        status = view["distance"] or view["stato_label"]
        color = "green" if force_green else (view["color"] or "")
        return (
            f'<a class="row {esc(color)}" href="/voce/{esc(view["id"])}">'
            f'<span class="mark">{ICONS[section]}</span>'
            f'<span class="row-copy"><strong>{esc(view["label"])}</strong>'
            f'{("<span class=\"sub\">" + esc(" · ".join(bits)) + "</span>") if bits else ""}'
            f"{extra}"
            f'{pill(status, color)}{_rincaro_badge(view)}</span></a>'
        )

    rows = [f"<h1>{esc(name)}</h1>"]
    open_items = [view for view in items if not view.get("paid_this_month")]
    paid_items = [view for view in items if view.get("paid_this_month")]
    rows.extend(row_html(view) for view in open_items)
    if paid_items:
        inner = "".join(row_html(view, force_green=True) for view in paid_items)
        rows.append(
            '<details class="paid-block">'
            f"<summary>Pagati questo mese <span class=\"count\">{len(paid_items)}</span></summary>"
            f'<div class="paid-rows">{inner}</div></details>'
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
        f'<p class="pills">{pill(status, color)}{_rincaro_badge(view)}</p>'
        + (f'<p class="banner rincaro">{esc(view["rincaro"]["text"])}</p>' if view.get("rincaro") else "")
        + f"<dl>{rows}</dl>"
        + f"{history}</article>{action}"
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
    rates = logic.get_rates()
    body = (
        f'<article class="card"><h1>Impostazioni</h1><p class="lead">{text}</p>'
        '<form method="post" action="/impostazioni/cambi">'
        f'<label>Cambio dollaro in baht<input name="rate_usd" inputmode="decimal" value="{esc(rates["rate_usd"])}"></label>'
        f'<label>Cambio euro in baht<input name="rate_eur" inputmode="decimal" value="{esc(rates["rate_eur"])}"></label>'
        "<button type=\"submit\">Salva cambi</button></form>"
        '<p><a href="/schema">Schema di importazione</a></p></article>'
    )
    return layout("Impostazioni", body, "")


def page_schema() -> str:
    raw = json.dumps(logic.IMPORT_EXAMPLE, ensure_ascii=False, indent=2)
    body = f'<article class="card"><h1>Schema di importazione</h1><pre>{esc(raw)}</pre></article>'
    return layout("Schema", body, "")


def _card_lines_html(lines: list[dict]) -> str:
    rows = []
    for line in lines:
        rows.append(
            f'<p class="date">{esc(line.get("name") or "")} · {esc(line.get("display") or "")}</p>'
        )
    return "".join(rows)


def page_spese(periodo: str = "mese", with_cards: bool = True) -> str:
    if periodo not in ("mese", "anno"):
        periodo = "mese"
    spese = logic.spending(periodo, with_cards)
    summary = logic.cards_summary()
    label_key = "year_label" if periodo == "anno" else "month_label"
    header_lines = [
        {"name": line["name"], "display": line.get(label_key) or ""}
        for line in summary.get("lines") or []
        if line.get(label_key)
    ]
    carte_flag = "1" if with_cards else "0"
    card_other = "0" if with_cards else "1"
    card_label = "Senza carte" if with_cards else "Con carte"
    categories = ""
    if spese.get("slices"):
        categories = donut_svg(spese["slices"]) + bar_rows(spese["slices"])
        if spese.get("total_thb"):
            categories += f'<p class="lead">Categorie {esc(logic.format_baht(spese["total_thb"]))}</p>'
    elif not with_cards or not spese.get("card_lines"):
        categories = '<p class="empty">Ancora nessun pagamento con importo.</p>'
    cards_block = ""
    if with_cards:
        cards = logic.list_cards()
        rows = []
        shown = {
            (line.get("id"), line.get("currency")): line.get("display") or ""
            for line in (spese.get("card_lines") or [])
        }
        for card in cards:
            current = shown.get((card["id"], card["currency"]), "")
            rows.append(
                f'<a class="row" href="/carte/{esc(card["id"])}"><span class="row-copy"><strong>{esc(card["name"])}</strong>'
                f'<span class="sub">{esc(card["kind"])} · {esc(card["currency"])}</span></span>'
                f'<strong>{esc(current)}</strong></a>'
            )
        period_lines = _card_lines_html(spese.get("card_lines") or []) or _card_lines_html(header_lines)
        period_lines = period_lines.replace('class="date"', 'class="quiet-line"')
        cards_block = (
            '<div class="spese-cards"><h2>Carte</h2>'
            f'{period_lines}{_out_charts_html(periodo)}{_analysis_html(periodo)}{"".join(rows)}'
            '<h2>Nuova carta</h2>'
            '<form method="post" action="/carte/nuova">'
            '<label>Nome<input name="name" required></label>'
            '<label>Tipo<select name="kind"><option value="credito">credito</option><option value="debito">debito</option></select></label>'
            '<label>Valuta<select name="currency"><option>THB</option><option>USD</option><option>EUR</option></select></label>'
            '<button type="submit">Aggiungi carta</button></form></div>'
        )
    body = (
        '<article class="card"><h1>Spese</h1>'
        '<p class="toggle">'
        f'<a class="{"on" if periodo != "anno" else ""}" href="/spese?periodo=mese&amp;carte={carte_flag}">Mese scorso</a>'
        f'<a class="{"on" if periodo == "anno" else ""}" href="/spese?periodo=anno&amp;carte={carte_flag}">Quest\'anno</a>'
        "</p>"
        '<p class="toggle">'
        f'<a href="/spese?periodo={esc(periodo)}&amp;carte={card_other}">{esc(card_label)}</a>'
        "</p>"
        f'<div class="spese-split"><div class="spese-cats"><h2>Categorie</h2>{categories}</div>{cards_block}</div>'
        "</article>"
    )
    return layout("Spese", body, "spese")


def _out_charts_html(periodo: str) -> str:
    blocks = []
    for chart in logic.card_out_charts(periodo):
        rows = []
        legend = []
        for bar in chart["bars"]:
            color = bar.get("color") or logic.CATEGORY_COLORS.get(bar["category"], "#5c5346")
            href = (
                "/spese/uscite?periodo="
                + quote(periodo)
                + "&valuta="
                + quote(chart["currency"])
                + "&categoria="
                + quote(bar["category"])
            )
            width = max(4, int(bar["percent"]))
            legend.append(
                f'<span><i class="swatch" style="background:{esc(color)}"></i>{esc(bar["category"])}</span>'
            )
            rows.append(
                f'<a class="cat-row" href="{esc(href)}">'
                f'<span class="cat-top"><span class="cat-name">{esc(bar["category"])}</span>'
                f'<span class="cat-meta">{int(bar["percent"])}% · {esc(bar.get("amount") or "")}</span></span>'
                f'<span class="bar"><span style="width:{width}%;background:{esc(color)}"></span></span>'
                "</a>"
            )
        if rows:
            blocks.append(
                f'<section class="cat-block"><h3>Categorie {esc(chart["currency"])}</h3>'
                f'<p class="legend">{"".join(legend)}</p>{"".join(rows)}</section>'
            )
    return "".join(blocks)


def _amount_bars(title: str, rows: list[dict], label_key: str, amount_key: str, text_key: str, color: str = "#1e6b45") -> str:
    if not rows:
        return ""
    peak = max(float(row.get(amount_key) or 0) for row in rows) or 1
    lines = []
    for row in rows:
        width = max(4, float(row.get(amount_key) or 0) / peak * 100)
        lines.append(
            '<div class="cat-row">'
            f'<span class="cat-top"><span class="cat-name">{esc(row.get(label_key) or "")}</span>'
            f'<span class="cat-meta">{esc(row.get(text_key) or "")}</span></span>'
            f'<span class="bar"><span style="width:{width:.1f}%;background:{esc(color)}"></span></span>'
            "</div>"
        )
    return f"<h3>{esc(title)}</h3>{''.join(lines)}"


def _analysis_html(periodo: str) -> str:
    blocks = []
    for block in logic.card_spend_analysis(periodo):
        currency = block["currency"]
        parts = []
        months = block.get("months") or []
        if months:
            labelled = []
            for item in months:
                index = int(item["month"][5:7]) - 1
                labelled.append({
                    "label": logic.MONTHS[index],
                    "amount": item["amount"],
                    "text": item["display"],
                })
            parts.append(_amount_bars(f"Andamento {currency}", labelled, "label", "amount", "text"))
        cards = block.get("cards") or []
        if len(cards) >= 1:
            parts.append(_amount_bars(
                f"Per carta {currency}",
                [{"label": item["name"], "amount": item["amount"], "text": f'{item["percent"]}% · {item["display"]}'} for item in cards],
                "label", "amount", "text",
            ))
            counts = "".join(
                f'<p class="quiet-line">{esc(item["name"])} · {int(item["count"])} movimenti</p>'
                for item in cards
            )
            parts.append(f"<h3>Movimenti {esc(currency)}</h3>{counts}")
        changes = block.get("changes") or []
        if changes:
            lines = []
            for item in changes:
                color = logic.CATEGORY_COLORS.get(item["category"], "#5c5346")
                if item["direction"] == "up":
                    sign = "+"
                elif item["direction"] == "down":
                    sign = "−"
                else:
                    sign = ""
                lines.append(
                    f'<p class="quiet-line"><i class="swatch" style="background:{esc(color)}"></i>'
                    f'{esc(item["category"])} · {sign}{int(item["percent"])}%</p>'
                )
            parts.append(f"<h3>Rispetto al mese prima · {esc(currency)}</h3>{''.join(lines)}")
        if parts:
            blocks.append(f'<section class="analysis">{"".join(parts)}</section>')
    return "".join(blocks)


def page_card_merchants(periodo: str, currency: str, category: str) -> str:
    rows = logic.card_category_transactions(periodo, currency, category)
    color = logic.CATEGORY_COLORS.get(category, "#5c5346")
    if rows:
        items = []
        for row in rows:
            when = logic.short_date(row["date"]) if row.get("date") and len(row["date"]) >= 10 else ""
            if when and not row["date"].startswith(str(logic.today_bkk().year)):
                when = f"{when} {row['date'][:4]}"
            items.append(
                "<li>"
                f'<span class="tx-main"><span class="who">{esc(row["merchant"])}</span>'
                f'<span class="meta">{esc(when)} · {esc(row["card"])}</span></span>'
                f'<strong>{esc(row["amount"])}</strong>'
                "</li>"
            )
        listing = f'<ul class="tx-list">{"".join(items)}</ul>'
    else:
        listing = '<p class="quiet-line">Ancora vuoto.</p>'
    body = (
        '<article class="card">'
        f'<p><a class="back" href="/spese?periodo={esc(periodo)}&amp;carte=1">Indietro</a></p>'
        f'<h1><i class="swatch" style="background:{esc(color)}"></i>{esc(category)}</h1>'
        f'<p class="quiet-line">{esc(currency)}</p>{listing}</article>'
    )
    return layout(category or "Uscite", body, "spese")


def page_cards() -> str:
    return page_spese("mese", True)


def page_card(card: dict) -> str:
    lines = []
    for month in card["months"]:
        bits = [month["month"], month.get("display") or ""]
        if month.get("opening"):
            bits.append("apertura " + str(month["opening"]))
        if month.get("money_in"):
            bits.append("entrate " + str(month["money_in"]))
        if month.get("money_out"):
            bits.append("uscite " + str(month["money_out"]))
        if month.get("closing"):
            bits.append("chiusura " + str(month["closing"]))
        lines.append(f'<li>{esc(" · ".join(bit for bit in bits if bit))}</li>')
        for tx in month.get("transactions") or []:
            last4 = f' •••• {tx["last4"]}' if tx.get("last4") else ""
            lines.append(
                f'<li>{esc(tx.get("date") or "")} {esc(tx.get("merchant") or "")} '
                f'{esc(tx.get("statement_amount") or "")} {esc(tx.get("statement_currency") or "")}{esc(last4)}</li>'
            )
    plastics = "".join(
        f'<li>•••• {esc(item["last4"])} {esc(item.get("label") or "")}</li>' for item in card.get("plastics") or []
    )
    plastic_block = f'<h2>Carte</h2><ul class="history">{plastics}</ul>' if plastics else ""
    history = plastic_block + ("<ul class=\"history\">" + "".join(lines) + "</ul>" if lines else "<p>Ancora vuoto.</p>")
    body = (
        f'<article class="card"><h1>{esc(card["name"])}</h1>'
        f'<p class="lead">{esc(card["kind"])} · {esc(card["currency"])}</p>{history}'
        '<h2>Modifica</h2>'
        f'<form method="post" action="/carte/{esc(card["id"])}/modifica">'
        f'<label>Nome<input name="name" value="{esc(card["name"])}" required></label>'
        f'<label>Tipo<select name="kind"><option value="credito"{" selected" if card["kind"]=="credito" else ""}>credito</option>'
        f'<option value="debito"{" selected" if card["kind"]=="debito" else ""}>debito</option></select></label>'
        '<button type="submit">Salva nome</button></form>'
        f'<form method="post" action="/carte/{esc(card["id"])}/elimina"><button type="submit">Elimina</button></form>'
        "</article>"
    )
    return layout(card["name"], body, "spese")


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
        if path == "/api/spese":
            query = parse_qs(urlparse(self.path).query)
            periodo = (query.get("periodo") or ["mese"])[0]
            carte = (query.get("carte") or ["0"])[0] == "1"
            self._json(200, logic.spending(periodo, carte))
            return
        if path == "/api/cards":
            if not self._write_allowed():
                self._json(401, {"error": "unauthorized"})
                return
            self._json(200, {"cards": logic.list_cards()})
            return
        if path.startswith("/api/cards/"):
            if not self._write_allowed():
                self._json(401, {"error": "unauthorized"})
                return
            card_id = path.removeprefix("/api/cards/").strip("/")
            if not card_id or "/" in card_id:
                self._json(404, {"error": "non trovato"})
                return
            found = next((card for card in logic.list_cards() if card["id"] == card_id), None)
            if found is None:
                self._json(404, {"error": "non trovato"})
                return
            self._json(200, found)
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
            query = parse_qs(urlparse(self.path).query)
            if query.get("spese"):
                periodo = (query.get("spese") or ["mese"])[0]
                carte = (query.get("carte") or ["1"])[0]
                self._redirect(f"/spese?periodo={periodo}&carte={carte}")
                return
            self._html(200, page_home())
            return
        if path == "/spese/uscite":
            query = parse_qs(urlparse(self.path).query)
            periodo = (query.get("periodo") or ["mese"])[0]
            currency = (query.get("valuta") or [""])[0]
            category = (query.get("categoria") or [""])[0]
            self._html(200, page_card_merchants(periodo, currency, category))
            return
        if path == "/spese":
            query = parse_qs(urlparse(self.path).query)
            periodo = (query.get("periodo") or query.get("spese") or ["mese"])[0]
            with_cards = (query.get("carte") or ["1"])[0] != "0"
            self._html(200, page_spese(periodo, with_cards))
            return
        if path == "/schema":
            self._html(200, page_schema())
            return
        if path == "/carte":
            self._redirect("/spese")
            return
        if path.startswith("/carte/"):
            card_id = path.removeprefix("/carte/").strip("/")
            if card_id and "/" not in card_id:
                found = next((card for card in logic.list_cards() if card["id"] == card_id), None)
                if found:
                    self._html(200, page_card(found))
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

    def _form_body(self) -> dict:
        raw = self._read_body()
        parsed = parse_qs(raw.decode("utf-8"), keep_blank_values=True)
        return {key: values[-1] for key, values in parsed.items()}

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
        if path == "/impostazioni/cambi":
            form = self._form_body()
            logic.set_rates(form.get("rate_usd"), form.get("rate_eur"))
            self._redirect("/impostazioni")
            return
        if path == "/carte/nuova":
            form = self._form_body()
            logic.create_card({"name": form.get("name"), "kind": form.get("kind"), "currency": form.get("currency")})
            self._redirect("/spese")
            return
        if path == "/carte/mese":
            form = self._form_body()
            if form.get("rate_usd") or form.get("rate_eur"):
                logic.set_rates(form.get("rate_usd"), form.get("rate_eur"))
            month = form.get("month")
            for card in logic.list_cards():
                payload = {"month": month, "currency": card["currency"]}
                filled = False
                for key in ("opening", "money_in", "money_out", "closing"):
                    value = form.get(f"{key}_{card['id']}", "")
                    if value != "":
                        payload[key] = value
                        filled = True
                if not filled:
                    continue
                logic.save_card_month(card["id"], payload)
            self._redirect("/spese")
            return
        if path.startswith("/carte/") and path.endswith("/mese"):
            card_id = path.removeprefix("/carte/").removesuffix("/mese").strip("/")
            form = self._form_body()
            logic.save_card_month(card_id, form)
            self._redirect(f"/carte/{card_id}")
            return
        if path.startswith("/carte/") and path.endswith("/modifica"):
            card_id = path.removeprefix("/carte/").removesuffix("/modifica").strip("/")
            form = self._form_body()
            logic.patch_card(card_id, form)
            self._redirect(f"/carte/{card_id}")
            return
        if path.startswith("/carte/") and path.endswith("/elimina"):
            card_id = path.removeprefix("/carte/").removesuffix("/elimina").strip("/")
            self._read_body()
            logic.delete_card(card_id)
            self._redirect("/spese")
            return
        if path == "/api/cards":
            if not self._write_allowed():
                self._read_body()
                self._json(401, {"error": "unauthorized"})
                return
            card = logic.create_card(self._json_body())
            self._json(201, card)
            return
        if path.startswith("/api/cards/") and path.endswith("/months"):
            if not self._write_allowed():
                self._read_body()
                self._json(401, {"error": "unauthorized"})
                return
            card_id = path.removeprefix("/api/cards/").removesuffix("/months").strip("/")
            saved = logic.save_card_month(card_id, self._json_body())
            if saved is None:
                self._json(404, {"error": "non trovato"})
                return
            self._json(200, saved)
            return
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
            if path.startswith("/api/cards/") and "/months/" in path:
                if not self._write_allowed():
                    self._json(401, {"error": "unauthorized"})
                    return
                card_id, _, month = path.removeprefix("/api/cards/").partition("/months/")
                if not logic.delete_card_month(card_id, month):
                    self._json(404, {"error": "non trovato"})
                    return
                self._json(200, {"ok": True})
                return
            if path.startswith("/api/cards/"):
                if not self._write_allowed():
                    self._json(401, {"error": "unauthorized"})
                    return
                card_id = path.removeprefix("/api/cards/")
                if not card_id or "/" in card_id:
                    self._json(404, {"error": "non trovato"})
                    return
                if not logic.delete_card(card_id):
                    self._json(404, {"error": "non trovato"})
                    return
                self._json(200, {"ok": True})
                return
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
            if path.startswith("/api/cards/"):
                if not self._write_allowed():
                    self._read_body()
                    self._json(401, {"error": "unauthorized"})
                    return
                card_id = path.removeprefix("/api/cards/")
                if not card_id or "/" in card_id:
                    self._read_body()
                    self._json(404, {"error": "non trovato"})
                    return
                card = logic.patch_card(card_id, self._json_body())
                if card is None:
                    self._json(404, {"error": "non trovato"})
                    return
                self._json(200, card)
                return
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
