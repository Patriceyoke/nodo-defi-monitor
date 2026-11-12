"""Watch-only Bitcoin security checks. Never asks for a seed or a private key."""
from __future__ import annotations

from typing import Any


def _item(owner: str, title: str, detail: str, tone: str, label: str) -> dict[str, Any]:
    return {"owner": owner, "title": title, "detail": detail, "tone": tone, "label": label}


def evaluate(wallets: list[dict[str, Any]], node: dict[str, Any], tor_enabled: bool | None = None) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []

    if not node.get("configured"):
        checks.append(
            _item(
                "tutti",
                "Nodo / Electrs",
                "L'indicizzatore locale non è impostato. In Impostazioni indica host e porta di Electrs o Fulcrum sul tuo Umbrel. I saldi Bitcoin restano vuoti finché non è collegato.",
                "warning",
                "Non configurato",
            )
        )
    elif not node.get("connected"):
        checks.append(
            _item(
                "tutti",
                "Nodo / Electrs",
                node.get("detail") or "L'indicizzatore locale non risponde. Controlla che il nodo Bitcoin e Electrs/Fulcrum siano avviati e allineati.",
                "danger",
                "Non collegato",
            )
        )
    else:
        height = node.get("height")
        extra = f" Altezza vista: {height}." if height else ""
        saved = str(node.get("detail") or "")
        if "Collegato via" in saved:
            text = saved
        elif node.get("host"):
            text = f"Collegato via {node['host']}:{node.get('port')}.{extra}"
        else:
            text = "Nodo Bitcoin / Electrs (locale) raggiungibile." + extra
        checks.append(
            _item(
                "tutti",
                "Nodo / Electrs",
                text,
                "success",
                "Sincronizzato" if not height else "Connesso",
            )
        )

    if tor_enabled is True:
        checks.append(
            _item(
                "tutti",
                "Tor",
                "Hai indicato che il nodo usa Tor. Aiuta a non rivelare le interrogazioni in chiaro. Questa app non verifica da sola il traffico Tor: il segno dipende dalla tua impostazione.",
                "success",
                "Attivo (dichiarato)",
            )
        )
    elif tor_enabled is False:
        checks.append(
            _item(
                "tutti",
                "Tor",
                "Hai indicato che Tor non è attivo sul nodo. Le interrogazioni all'indicizzatore restano sulla rete locale. Puoi accenderlo dal nodo Bitcoin di Umbrel se lo desideri.",
                "warning",
                "Non dichiarato",
            )
        )
    else:
        checks.append(
            _item(
                "tutti",
                "Tor",
                "Stato Tor non dichiarato. L'app non può vederlo da sola: puoi indicarlo in Impostazioni. Il promemoria è solo informativo.",
                "warning",
                "Non dichiarato",
            )
        )

    if not wallets:
        checks.append(
            _item(
                "tutti",
                "Backup della seed",
                "Ricorda di verificare offline, da solo, che il backup della seed esista e sia leggibile. L'app non chiede e non deve mai vedere seed o chiavi private.",
                "warning",
                "Promemoria",
            )
        )
        return checks

    for w in wallets:
        owner = w["owner"]
        name = w["label"]
        reuse = w.get("reuse") or []
        if reuse:
            checks.append(
                _item(
                    owner,
                    f"Riutilizzo di indirizzi · {name}",
                    f"Su {name} l'app ha visto {len(reuse)} indirizzo/i già usato/i per più di una transazione. Meglio usarne uno nuovo per le prossime ricezioni. Per questo controllo non serve alcuna seed.",
                    "warning",
                    "Rilevato",
                )
            )
        else:
            if w.get("connected"):
                checks.append(
                    _item(
                        owner,
                        f"Riutilizzo di indirizzi · {name}",
                        f"Nessun riutilizzo evidente sugli indirizzi derivati di {name}.",
                        "success",
                        "Nella norma",
                    )
                )

        dust = w.get("dust") or []
        if dust:
            sat = dust[0]["sats"]
            checks.append(
                _item(
                    owner,
                    f"Polvere / UTXO sconosciuto · {name}",
                    f"Ricevuti {sat} sat su {name}: possibile dusting. Non spendere quell'uscita insieme ad altri fondi. Per questo controllo non serve alcuna seed.",
                    "warning",
                    "Da osservare",
                )
            )

        # Unexpected outgoing: if alert_out_btc is set, flag sends above it
        alert_out = w.get("alert_out_btc")
        big_out = []
        if alert_out is not None:
            for tx in w.get("txs") or []:
                if tx.get("kind") == "Inviato" and float(tx.get("btc") or 0) >= float(alert_out):
                    big_out.append(tx)
        if alert_out is not None and big_out:
            checks.append(
                _item(
                    owner,
                    f"Uscite inattese · {name}",
                    f"Su {name} c'è almeno un'uscita da {big_out[0]['btc']:.8f} BTC, sopra la soglia di {float(alert_out):.8f} BTC che hai impostato. Controlla se la riconosci. La firma resta tua.",
                    "danger",
                    "Sopra soglia",
                )
            )
        elif w.get("connected"):
            checks.append(
                _item(
                    owner,
                    f"Uscite inattese · {name}",
                    f"Nessuna uscita fuori soglia vista su {name}."
                    + (f" Soglia impostata: {float(alert_out):.8f} BTC." if alert_out is not None else " (nessuna soglia di uscita impostata)"),
                    "success",
                    "Nella norma",
                )
            )

        min_bal = w.get("alert_min_btc")
        if min_bal is not None and w.get("connected") and w.get("btc", 0) < float(min_bal) and w.get("btc", 0) > 0:
            checks.append(
                _item(
                    owner,
                    f"Saldo sotto soglia · {name}",
                    f"{name} ha {w['btc']:.8f} BTC, sotto la soglia di {float(min_bal):.8f} BTC.",
                    "warning",
                    "Sotto soglia",
                )
            )

        # Zero-balance drain: wallet now 0 but had outgoing history
        if w.get("connected") and w.get("btc", 0) == 0 and any(tx.get("kind") == "Inviato" for tx in (w.get("txs") or [])):
            checks.append(
                _item(
                    owner,
                    f"Svuotamento a saldo zero · {name}",
                    f"{name} risulta a saldo zero e mostra uscite recenti. Se non lo aspettavi, verifica offline. L'app non può spendere e non chiede la seed.",
                    "danger",
                    "Allarme",
                )
            )

        # Hot vs cold: if this wallet is marked spesa/hot and exceeds threshold
        role = (w.get("role") or "watch").lower()
        hot_thr = w.get("hot_threshold_btc")
        if role in {"spesa", "hot"} and hot_thr is not None and w.get("btc", 0) > float(hot_thr):
            checks.append(
                _item(
                    owner,
                    f"Saldo sulla Spesa · {name}",
                    f"{name} ha {w['btc']:.8f} BTC, sopra la soglia di {float(hot_thr):.8f} BTC rispetto al cold storage. Valuta di spostare l'eccesso a freddo; la firma resta tua.",
                    "warning",
                    "Sopra soglia",
                )
            )

    # Drain across all
    if any(c["label"] == "Allarme" and "Svuotamento" in c["title"] for c in checks):
        pass
    else:
        if any(w.get("connected") for w in wallets):
            checks.append(
                _item(
                    "tutti",
                    "Svuotamento a saldo zero",
                    "Nessun conto osservato risulta svuotato di colpo.",
                    "success",
                    "Nessun allarme",
                )
            )

    # Large amounts should live in cold/multisig
    by_owner: dict[str, list[dict[str, Any]]] = {}
    for w in wallets:
        by_owner.setdefault(w["owner"], []).append(w)
    for owner, rows in by_owner.items():
        if not any(w.get("connected") for w in rows):
            continue
        total = sum(float(w.get("btc") or 0) for w in rows)
        if total <= 0:
            continue
        coldish = [w for w in rows if (w.get("role") or "").lower() in {"cold", "freddo", "multisig", "watch"} or "cold" in w["label"].lower() or "multi" in w["label"].lower() or "fredd" in w["label"].lower()]
        hotish = [w for w in rows if (w.get("role") or "").lower() in {"spesa", "hot"} or "spesa" in w["label"].lower()]
        cold_amt = sum(float(w.get("btc") or 0) for w in coldish)
        hot_amt = sum(float(w.get("btc") or 0) for w in hotish)
        if hotish and coldish and hot_amt > cold_amt and hot_amt > 0:
            checks.append(
                _item(
                    owner,
                    "Somme grandi a freddo",
                    f"Su questo titolare la parte più grande ({hot_amt:.8f} BTC) sta su un conto di spesa, non a freddo o in multisig. Per importi importanti è più prudente il cold storage o il multisig. La firma resta tua.",
                    "warning",
                    "Da valutare",
                )
            )
        elif coldish and (not hotish or cold_amt >= hot_amt):
            checks.append(
                _item(
                    owner,
                    "Somme grandi a freddo",
                    "La parte più grande di questo titolare sta in un conto etichettato come freddo o multisig, non su un conto di spesa.",
                    "success",
                    "Nella norma",
                )
            )

    checks.append(
        _item(
            "tutti",
            "Backup della seed",
            "Ricorda di verificare offline, da solo, che il backup della seed esista e sia leggibile. L'app non chiede e non deve mai vedere seed o chiavi private.",
            "warning",
            "Promemoria",
        )
    )
    return checks
