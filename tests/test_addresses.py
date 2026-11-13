"""Address export and import. Examples are fake; nothing here is a real wallet."""
import pytest

from app import db
from app.config import APP_VERSION


@pytest.fixture(scope="module", autouse=True)
def _restore_wallets_after_module():
    db.init_db()
    before = {row["id"] for row in db.list_evm()}
    yield
    for row in db.list_evm():
        if row["id"] not in before:
            db.delete_evm(row["id"])

IO = "0x" + "11" * 20
MOGLIE = "0x" + "22" * 20
OTHER = "0x" + "33" * 20
SECRET = "0x" + "ab" * 32


def test_index_and_static_are_not_cached(client):
    page = client.get("/")
    assert page.status_code == 200
    assert page.headers["cache-control"] == "no-store"
    html = page.text
    assert f"/static/app.js?v={APP_VERSION}" in html
    assert f"/static/styles.css?v={APP_VERSION}" in html
    assert f"/static/vendor/chart.umd.min.js?v={APP_VERSION}" in html
    assert f"/static/favicon.svg?v={APP_VERSION}" in html
    assert "Disinstallare cancella i dati" in html
    assert "0x1111111111111111111111111111111111111111, io, Esempio" in html
    script = client.get("/static/app.js")
    assert script.status_code == 200
    assert script.headers["cache-control"] == "no-cache"
    assert APP_VERSION == "0.5.4"


def test_export_is_addresses_only(client):
    db.add_evm("Esempio", IO, "io")
    res = client.get("/api/addresses/export")
    assert res.status_code == 200
    body = res.json()
    assert set(body) == {"kind", "version", "addresses"}
    assert body["kind"] == "nodo-defi-monitor-addresses"
    row = next(item for item in body["addresses"] if item["address"] == IO)
    assert row == {"address": IO, "owner": "io", "label": "Esempio"}
    assert "settings" not in body
    assert "nft" not in res.text
    assert "mcp" not in res.text


def test_import_text_skips_duplicates_and_secrets(client):
    if not any(row["address"] == IO and row["owner"] == "io" for row in db.list_evm()):
        db.add_evm("Esempio", IO, "io")
    before = {row["address"] for row in db.list_evm()}
    previous_key = db.get_setting("nft_api_key")
    db.set_setting("nft_api_key", "keep-me")
    text = "\n".join([
        f"{IO}, io, Di nuovo",
        f"{MOGLIE}, moglie",
        f"{OTHER}, Io, Altro titolare",
        "non-un-indirizzo, io",
        f"{SECRET}, io, Non salvare",
        "alfa beta gamma delta epsilon zeta eta theta iota kappa lambda mu",
        "",
        "# commento",
    ])
    res = client.post("/api/addresses/import", json={
        "text": text,
        "settings": {"nft_api_key": "nope", "eth_rpc_url": "http://evil.example"},
    })
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["added"] == 2
    assert body["skipped"] == 1
    assert body["invalid"] == 3
    assert SECRET not in res.text
    assert "abab" not in res.text
    saved = {row["address"]: row for row in db.list_evm()}
    assert MOGLIE in saved and saved[MOGLIE]["owner"] == "moglie"
    assert saved[MOGLIE]["label"] == "Importato"
    assert saved[OTHER]["owner"] == "io"
    assert SECRET not in saved
    assert IO in saved and saved[IO]["label"] == "Esempio"
    assert db.get_setting("nft_api_key") == "keep-me"
    assert db.get_setting("eth_rpc_url") != "http://evil.example"
    db.set_setting("nft_api_key", previous_key)
    again = client.post("/api/addresses/import", json={"text": f"{MOGLIE}, moglie, Seconda"})
    assert again.json()["added"] == 0
    assert again.json()["skipped"] == 1
    fresh = {row["address"]: row for row in db.list_evm()}
    assert fresh[MOGLIE]["label"] == "Importato"
    assert before <= set(fresh)


def test_import_json_file_adds_without_deleting(client):
    kept = "0x" + "44" * 20
    db.add_evm("Resta", kept, "moglie")
    payload = {
        "kind": "nodo-defi-monitor-addresses",
        "version": 1,
        "addresses": [
            {"address": "0x" + "55" * 20, "owner": "io", "label": "Da file"},
            {"address": kept, "owner": "moglie", "label": "Doppione"},
        ],
    }
    res = client.post("/api/addresses/import", json=payload)
    assert res.status_code == 200, res.text
    assert res.json()["added"] == 1
    assert res.json()["skipped"] == 1
    rows = {row["address"]: row for row in db.list_evm()}
    assert rows[kept]["label"] == "Resta"
    assert rows["0x" + "55" * 20]["label"] == "Da file"
