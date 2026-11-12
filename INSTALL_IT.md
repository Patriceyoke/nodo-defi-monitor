# Installazione di Monitor DeFi su Umbrel

L'app è in **sola lettura**. Non chiede e non deve mai vedere seed o chiavi private. Le transazioni, se un giorno vorrai farle, le firmi tu sul tuo portafoglio hardware (per esempio OneKey).

Oggi l'app è pronta in locale. Per installarla sul tuo Umbrel serve **una** di queste due strade. La strada A è quella “da App Store”. La strada B è più immediata se hai accesso SSH o Portainer.

Sostituisci sempre `patriceyoke` con il tuo utente GitHub in minuscolo, quando pubblichi le immagini.

## Prima di partire

- Umbrel aggiornato (umbrelOS 2.0 o successivo, per gli agenti MCP).
- Per il modulo Bitcoin: app **Bitcoin** e **Electrs** (o Fulcrum) installate e sincronizzate. Non sono un requisito obbligatorio: senza di esse la DeFi funziona lo stesso, i saldi Bitcoin restano vuoti.
- Hermes Agent già installato se vuoi farlo interrogare l'app.
- Nessun dato personale è presente in questo codice. Gli indirizzi e gli xpub li inserisci tu, solo sul tuo server.

## Strada A — Community App Store (consigliata, richiede GitHub)

Umbrel non costruisce l'immagine sul dispositivo: la **scarica** da un registro (qui GHCR). Serve quindi:

1. Un account GitHub.
2. Un repository **pubblico** con il codice dell'app (senza dati personali).
3. Un repository **pubblico** del Community App Store, oppure lo stesso repo se `umbrel-app-store.yml` sta nella radice.
4. Il consenso a pubblicare il codice e a far costruire le immagini `linux/amd64` e `linux/arm64`.

Passi:

1. Crea un repo pubblico, per esempio `nodo-defi-monitor`, e (consigliato) un secondo repo `nodo-app-store`.
2. Nel codice, sostituisci ogni `ghcr.io/patriceyoke/` e ogni `https://github.com/Patriceyoke/` con il tuo utente GitHub in minuscolo.
3. Carica il codice dell'app nel primo repo. Nella cartella `umbrel-store/` trovi già il manifesto del negozio: il contenuto di `umbrel-store/` va nella radice del repo `nodo-app-store` (così `umbrel-app-store.yml` è in cima).
4. Su GitHub, in **Settings > Actions > General**, consenti le Actions e la scrittura dei pacchetti. La prima pubblicazione di `ghcr.io/<tuo-utente>/nodo-defi-monitor` deve essere **public**.
5. Lancia il workflow **Publish multi-arch image** (o crea il tag `v0.1.0`). Attendi che risultino le piattaforme `linux/amd64` e `linux/arm64`.
6. Aggiorna `umbrel-store/nodo-defi-monitor/docker-compose.yml` con il nome immagine vero, se non l'hai già fatto.
7. Sul tuo Umbrel: **App Store → ⋯ → Community App Stores →** incolla l'URL del repo `nodo-app-store` (es. `https://github.com/<tuo-utente>/nodo-app-store`).
8. Installa **Monitor DeFi**. Aprila dalla home. Vai a **Impostazioni**.

Questa strada **non si completa da sola** finché il repo e l'immagine non sono pubblici. Il codice è già pronto, ma non è stato pubblicato da nessuno: serve il tuo account e il tuo consenso.

## Strada B — Installazione manuale con Docker (possibile già a mezzogiorno)

Se puoi aprire una sessione SSH sul tuo Umbrel, oppure usi Portainer:

1. Copia la cartella del progetto sul dispositivo (chiavetta, `scp`, o un archivio).
2. Nella cartella del progetto:

```bash
docker compose up -d --build
```

3. Apri `http://umbrel.local:3847` (o l'IP del tuo Umbrel, porta **3847**).

La prima costruzione sul dispositivo richiede qualche minuto. I dati restano in `./data`. Questa strada non passa dall'App Store e non richiede GitHub.

## Prima configurazione (dopo l'avvio)

1. Apri Monitor DeFi → **Impostazioni**.
2. (Facoltativo) Inserisci l'URL del tuo RPC Ethereum locale. Se lo lasci vuoto, l'app usa un RPC pubblico.
3. Per Bitcoin: host e porta di Electrs o Fulcrum sul tuo Umbrel (di solito porta `50001`). Su molte installazioni l'host interno è simile a `electrs_electrs_1`. Se non sei sicuro, prova prima dalla rete locale e, se non collega, controlla il nome del contenitore Electrs.
4. Aggiungi solo **indirizzi Ethereum pubblici** e **xpub / zpub / descrittori** di sola osservazione, indicando per ciascuno il titolare **Io** o **Moglie**. Puoi etichettare un conto Bitcoin come Cold storage, Multisig o Spesa e impostare le soglie di avviso.
5. Se incolli una seed o una chiave privata, l'app la **rifiuta**. Non farlo.
6. Premi **Aggiorna** nella dashboard. I selettori **Io / Moglie / Combinato** filtrano tutta la pagina.

## Collegare Hermes tramite MCP

Ci sono due livelli, distinti:

### 1. Agenti MCP di umbrelOS (consigliato)

1. Sul tuo Umbrel, entra in **Impostazioni → AI agents (MCP)**. La funzione è in beta: concedi l'accesso solo a un agente che controlli tu.
2. Solo l'account proprietario può configurarla. Gli agenti agiscono come il proprietario.
3. Segui i passi per **Hermes**. Copia il token: umbrelOS lo mostra **una sola volta**.
4. L'endpoint è `http://umbrel.local/mcp` (HTTP in chiaro, anche se apri umbrelOS in HTTPS).
5. Concedi all'agente l'accesso all'app **Monitor DeFi**. Puoi revocarlo in qualsiasi momento.
6. Questo accesso permette a Hermes di usare gli strumenti di umbrelOS sull'app (stato, dati, riavvio, ecc.). Non è automatico che umbrelOS pubblichi anche i tool interni dell'app (`get_portfolio`, `get_btc_wallets`, …). Per quelli serve il punto 2.

### 2. Server MCP dell'app (i tool di portafoglio)

1. In Monitor DeFi → Impostazioni, genera il **token MCP** dell'app e copialo subito.
2. Aggiungi il frammento in `hermes/config-snippet.yaml` al `config.yaml` di Hermes:
   - Hermes in locale: `~/.hermes/config.yaml`
   - Hermes come app Umbrel: **File → App → Hermes Agent → data → hermes → config.yaml**
3. Sostituisci i due `Bearer` con i token veri. Riavvia Hermes.
4. Prova a chiedere, per esempio: «Mostrami il portafoglio combinato» oppure «Ci sono switch convenienti per Io, dopo il gas?».

I tool disponibili, tutti con filtro `owner=io|moglie|tutti`, sono:

- `get_portfolio`, `get_balances`, `get_apys`, `check_switch`
- `get_alerts`, `get_history`, `get_btc_wallets`, `get_btc_security`

Non esistono tool di firma, invio o trasferimento.

## Dopo l'installazione, controlli rapidi

- La scritta **sola lettura** è visibile.
- I totali Io, Moglie e Combinato restano in alto anche quando filtri la vista.
- Se Electrs è spento, le carte Bitcoin lo dicono chiaramente e i controlli di sicurezza restano in sola osservazione (incluso il promemoria sul backup della seed, che fai tu, offline).
- Lo storico si accumula in SQLite, solo sul tuo server.

## Aggiornamenti

- Strada A: pubblica una nuova immagine e alza la versione in `umbrel-app.yml`, poi aggiorna dall'App Store.
- Strada B: aggiorna i file e rilancia `docker compose up -d --build`.
