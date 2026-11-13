"""Urgent alerts are only the problems that need a fix. Examples are fake."""
from app.security import (
    annotate_light_alerts,
    ens_is_urgent,
    incident_is_urgent,
    protocol_is_urgent,
)


def test_only_a_broken_peg_or_an_expired_name_is_urgent():
    assert protocol_is_urgent({"tone": "danger", "label": "Peg allontanato", "title": "Peg USDC"})
    assert not protocol_is_urgent({"tone": "danger", "label": "Utilizzo molto alto", "title": "Aave"})
    assert not protocol_is_urgent({"tone": "success", "label": "Nella norma", "title": "Aave"})
    assert not protocol_is_urgent({"tone": "warning", "label": "Da osservare", "title": "Peg DAI"})
    assert ens_is_urgent({"level": "danger"})
    assert not ens_is_urgent({"level": "warning"})
    assert incident_is_urgent({"severity": "danger", "detail": "in pausa"})
    assert not incident_is_urgent({"severity": "warning", "detail": "incidente vecchio"})


def test_light_alerts_keep_the_approval_key_and_skip_a_calm_protocol():
    approval = "0x" + "11" * 20 + "|ethereum|0x" + "22" * 20 + "|0x" + "33" * 20
    protocols = [
        {"protocol": "USDC", "title": "Peg USDC", "label": "Peg allontanato", "tone": "danger"},
        {"protocol": "Aave", "title": "Aave", "label": "Nella norma", "tone": "success"},
    ]
    ens = [
        {"owner": "io", "name": "esempio.eth", "level": "danger"},
        {"owner": "io", "name": "fra.eth", "level": "warning"},
    ]
    summaries = {"io": {"urgent_keys": [approval]}, "moglie": {"urgent_keys": []}, "tutti": {"urgent_keys": []}, "view": {"urgent_keys": []}}
    positions = [{"owner": "io", "protocol": "Aave", "asset": "USDC"}]
    annotate_light_alerts(protocols, ens, summaries, positions, "tutti")
    assert protocols[0]["urgent"] is True
    assert protocols[1]["urgent"] is False
    assert ens[0]["urgent"] is True
    assert ens[1]["urgent"] is False
    io_keys = summaries["io"]["urgent_keys"]
    assert approval in io_keys
    assert any(key.startswith("protocol|peg|usdc|") for key in io_keys)
    assert any(key.startswith("ens|io|esempio.eth|") for key in io_keys)
    assert not any(key.startswith("ens|") and "fra.eth" in key for key in io_keys)
    assert summaries["moglie"]["urgent_keys"] == []
    assert summaries["io"]["urgent"] == len(io_keys)
