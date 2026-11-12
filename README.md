# Monitor DeFi

Read-only dashboard for Ethereum DeFi positions (Lido, Aave v3, Compound, Fluid, Spark/Sky) and Bitcoin watch-only accounts (xpub / zpub / descriptors via a local Electrs or Fulcrum indexer). Every address or xpub has an owner tag (`io` or `moglie`). A read-only MCP server lets an agent such as Hermes query the portfolio. The app never accepts seed phrases or private keys. It never signs or sends transactions.

This repository contains **no personal data, no wallet addresses, and no extended keys**. You add those locally in the Settings UI.

## Quick start (development)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m uvicorn app.main:app --host 127.0.0.1 --port 3847
```

Open http://127.0.0.1:3847 — then add only public Ethereum addresses and watch-only Bitcoin keys.

## HTTP routes

`GET /api` lists them. The ones used to see why the dashboard is empty:

| Method | Path | What it does |
|---|---|---|
| GET | `/api/health` | Process is up. Does not read wallets. |
| GET | `/api/portfolio?owner=tutti` | Last portfolio. `refresh=true` forces a new read. |
| POST | `/api/refresh?owner=tutti` | Force a read and return the portfolio, including `errors`. This is what Aggiorna calls. |
| GET | `/api/alerts` | Last alerts and refresh errors. Does not start a scan, so it opens even when a read is slow. |
| GET | `/api/debug/refresh-status` | Last refresh time, the RPC used on each chain, and per-wallet errors. |

```bash
python -m pytest -q
```

## Umbrel

See [INSTALL_IT.md](INSTALL_IT.md) (Italian step-by-step, including Hermes). Packaging lives in `umbrel-store/`. Replace `patriceyoke` before publishing images to GHCR.

## License

MIT. Third-party libraries keep their own licenses (see `requirements.txt`). Chart.js is vendored under its MIT license in `web/vendor/`.
