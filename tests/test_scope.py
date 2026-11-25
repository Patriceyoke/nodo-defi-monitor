"""The scope selector keeps Io, Moglie, Combinato, and one wallet by name."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run(script: str):
    completed = subprocess.run(
        ["node", "-e", script],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def test_scope_switches_wallet_and_clears_back():
    result = _run(
        """
        const scope = require("./web/scope.js");
        let state = { owner: "tutti", wallet: "" };
        state = scope.applyOwner(state, "io");
        if (state.owner !== "io" || state.wallet !== "") throw new Error("owner");
        state = scope.selectWallet(state, "io::Demo Jade");
        const rows = [
          { owner: "io", label: "Demo Jade", asset: "A" },
          { owner: "io", label: "Demo T1", asset: "B" },
          { owner: "moglie", label: "Demo N1", asset: "C" },
        ];
        const visible = rows.filter((row) => scope.inScope(state, row)).map((row) => row.asset);
        state = scope.clearWallet(state);
        const restored = rows.filter((row) => scope.inScope(state, row)).map((row) => row.label);
        console.log(JSON.stringify({ visible, owner: state.owner, restored }));
        """
    )
    assert result["visible"] == ["A"]
    assert result["owner"] == "io"
    assert result["restored"] == ["Demo Jade", "Demo T1"]


def test_scope_persists_in_the_query_and_storage():
    result = _run(
        """
        const scope = require("./web/scope.js");
        const state = { owner: "moglie", wallet: "moglie::Demo N1" };
        const query = scope.persistQuery(state);
        const fromUrl = scope.readScope("?" + query, "");
        const fromStore = scope.readScope("", JSON.stringify(state));
        console.log(JSON.stringify({ query, fromUrl, fromStore }));
        """
    )
    assert "vista=moglie" in result["query"]
    assert "portafoglio=" in result["query"]
    assert result["fromUrl"] == {"owner": "moglie", "wallet": "moglie::Demo N1"}
    assert result["fromStore"] == result["fromUrl"]


def test_card_titles_name_the_view_without_an_address():
    result = _run(
        """
        const scope = require("./web/scope.js");
        const combined = scope.cardTitle("Andamento", { owner: "tutti", wallet: "" });
        const person = scope.cardTitle("Ripartizione", { owner: "moglie", wallet: "" });
        const wallet = scope.cardTitle("Andamento", { owner: "io", wallet: "io::Demo Jade" });
        console.log(JSON.stringify({ combined, person, wallet }));
        """
    )
    assert result == {
        "combined": "Andamento · Combinato",
        "person": "Ripartizione · Moglie",
        "wallet": "Andamento · Demo Jade",
    }
    owner = _run(
        """
        const scope = require("./web/scope.js");
        console.log(JSON.stringify({
          combined: scope.historyOwner({ owner: "tutti", wallet: "" }),
          wallet: scope.historyOwner({ owner: "tutti", wallet: "io::Demo Jade" })
        }));
        """
    )
    assert owner == {"combined": "combinato", "wallet": "io"}


def test_ripartizione_comes_before_andamento():
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    assert html.index('id="split"') < html.index('id="trend"')
    assert "Andamento · Combinato" in html
    assert "Ripartizione · Combinato" in html


def test_dashboard_selector_replaces_the_wallet_tiles():
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    assert 'id="wallet-menu"' in html
    assert 'id="wallet-clear"' in html
    assert "Tocca un portafoglio" not in html
    assert "<h2>Portafogli</h2>" not in html
    assert "scope.js?v=" in html
