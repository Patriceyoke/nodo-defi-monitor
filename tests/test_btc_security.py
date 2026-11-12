
from app.btc_security import evaluate


def test_node_and_backup_and_reuse_and_dust():
    node = {"configured": True, "connected": True, "height": 900000}
    wallets = [
        {
            "owner": "io",
            "label": "Spesa",
            "role": "spesa",
            "connected": True,
            "btc": 0.2,
            "hot_threshold_btc": 0.1,
            "alert_out_btc": 0.05,
            "reuse": ["bc1qexample"],
            "dust": [{"address": "bc1qdust", "sats": 546}],
            "txs": [{"kind": "Inviato", "btc": 0.01}],
        },
        {
            "owner": "io",
            "label": "Cold storage",
            "role": "cold",
            "connected": True,
            "btc": 0.5,
            "reuse": [],
            "dust": [],
            "txs": [],
        },
    ]
    checks = evaluate(wallets, node, tor_enabled=True)
    titles = [c["title"] for c in checks]
    assert any("Nodo / Electrs" in t and c["tone"] == "success" for c, t in zip(checks, titles))
    assert any(c["title"] == "Tor" and c["tone"] == "success" for c in checks)
    assert any("Riutilizzo" in c["title"] and c["tone"] == "warning" for c in checks)
    assert any("Polvere" in c["title"] for c in checks)
    assert any("Saldo sulla Spesa" in c["title"] and c["tone"] == "warning" for c in checks)
    assert any(c["title"] == "Backup della seed" and "non deve mai vedere" in c["detail"] for c in checks)
    assert any(c["title"] == "Somme grandi a freddo" and c["tone"] == "success" for c in checks)


def test_drain_alarm():
    node = {"configured": True, "connected": True}
    wallets = [
        {
            "owner": "moglie",
            "label": "Multisig",
            "role": "multisig",
            "connected": True,
            "btc": 0,
            "reuse": [],
            "dust": [],
            "txs": [{"kind": "Inviato", "btc": 0.3}],
        }
    ]
    checks = evaluate(wallets, node)
    assert any("Svuotamento" in c["title"] and c["tone"] == "danger" for c in checks)


def test_unconfigured_node():
    checks = evaluate([], {"configured": False, "connected": False})
    assert any(c["label"] == "Non configurato" for c in checks)
