# Casa Viale

Scadenze di casa, garage, documenti e visto. L'interfaccia è in italiano e le date usano il fuso Asia/Bangkok. L'app non comanda casa, cancello, stampante, rete o crypto.

## Installazione

Questo repository è già lo store Nodo (`umbrel-app-store.yml`, id `nodo`). Se nello store compare Nodo, non va aggiunto di nuovo.

Altrimenti, sull'Umbrel: **App Store → tre puntini → Community App Stores** e incolla l'URL del repository `https://github.com/Patriceyoke/nodo-defi-monitor`.

Poi installa **Casa Viale** (id `nodo-casa`, porta 8795).

Umbrel scarica `ghcr.io/patriceyoke/nodo-casa:1.2.3`. Il proxy punta al contenitore `nodo-casa_web_1`.

Dopo «Segna pagato» o «Segna fatto», le scadenze non mensili passano alla data successiva. Un rincaro oltre la soglia (di solito +15%) compare in home. Spese mostra le categorie in baht e, accanto, ogni carta nella sua valuta. Non si sommano baht e dollari e non si salva mai un numero di carta.

## Dati

Il database nasce vuoto. Sul dispositivo sta in `/data/casa.db`. Il token dell'agente sta in `/data/agent.token` (permessi 600): non viene mostrato, non finisce nei log e non è nell'immagine. Nessuno dei due file è in questo repository.

## Prove

Immagine `nodo-casa:1.0.0`, volume `/data` vuoto, 21 novembre 2026.

1. `docker build` e avvio: ok. Il processo gira come utente 1000 e ascolta sulla porta 8795. Il compose dello store non pubblica porte.
2. `GET /api/health` → 200 `{"ok": true}`.
3. La home in italiano mostra Casa, Garage, Documenti, Immigrazione e «Ancora vuoto.». Nessuna data di esempio.
4. `POST /api/import` senza Bearer → 401 e zero righe. Anche un Bearer sbagliato risponde 401 e non scrive.
5. Con il Bearer si crea una voce di prova. La home la mostra (`1 dic`, `tra 10 giorni`). `POST /api/items/{id}/done`, e anche il bottone Conferma, la segnano fatta.
6. La voce di prova è stata tolta dal database locale prima del commit. La home torna a «Ancora vuoto.».
7. Riavvio del container: restano 0 voci. Il database e il token sono solo nel volume locale. `git status` non li elenca.
8. In `nodo-casa` non ci sono email. L'unico campo vuoto del manifesto Umbrel è `defaultPassword`.
