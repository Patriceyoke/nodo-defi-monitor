# Casa Viale

Scadenze di casa, garage, documenti e visto. L'interfaccia è in italiano e le date usano il fuso Asia/Bangkok. L'app non comanda casa, cancello, stampante, rete o crypto.

## Installazione

Questo repository è già lo store Nodo (`umbrel-app-store.yml`, id `nodo`). Se nello store compare Nodo, non va aggiunto di nuovo.

Altrimenti, sull'Umbrel: **App Store → tre puntini → Community App Stores** e incolla l'URL del repository `https://github.com/Patriceyoke/nodo-defi-monitor`.

Poi installa **Casa Viale** (id `nodo-casa`, porta 8795).

Umbrel scarica `ghcr.io/patriceyoke/nodo-casa:1.2.6`. Il proxy punta al contenitore `nodo-casa_web_1`.

Dopo «Segna pagato» o «Segna fatto», le scadenze non mensili passano alla data successiva. Un rincaro oltre la soglia (di solito +15%) compare in home. La home elenca anche, entro 90 giorni, il visto, il rapporto dei 90 giorni e i documenti in scadenza. Spese mostra le categorie in baht e, accanto, ogni carta nella sua valuta. Baht e dollari non si sommano. Le ultime quattro cifre restano nel database per il confronto degli estratti, e non compaiono nelle pagine né in `GET /api/export`.

## Dati

Il database nasce vuoto. Sul dispositivo sta in `/data/casa.db`, in modalità WAL. Il token dell'agente sta in `/data/agent.token` (permessi 600): non viene mostrato, non finisce nei log e non è nell'immagine. Nessuno dei due file è in questo repository.

Ogni importazione riuscita scrive una copia accanto al database (`casa.db.bak.1`, poi `.2` e `.3`). Un estratto identico non riscrive i movimenti. Un elenco vuoto non cancella il mese già salvato.

`GET /api/health` risponde `{"ok": true}`. Il contenitore gira come utente 1000 e il controllo di salute usa quella stessa risposta.
