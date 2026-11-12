/* Monitor DeFi frontend — original, Italian, read-only. */
const PALETTE = ["#0D9488", "#2563EB", "#F59E0B", "#7C3AED", "#06B6D4", "#E11D48", "#64748B"];
const OWNER_LABEL = { io: "Io", moglie: "Moglie", tutti: "Combinato" };
const state = { owner: "tutti", period: "30", currency: "USD", alloc: "protocollo", btcTab: "all", data: null, fx: 0.92, nfts: null, showHiddenNfts: false };
const charts = {};

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => [...document.querySelectorAll(sel)];

function money(usd, cur = state.currency) {
  const n = cur === "EUR" ? usd * (state.fx || 0.92) : usd;
  return n.toLocaleString("it-IT", { maximumFractionDigits: 0 }) + (cur === "EUR" ? " €" : " $");
}
function pct(n) {
  if (n == null || Number.isNaN(n)) return "—";
  return Number(n).toLocaleString("it-IT", { minimumFractionDigits: 1, maximumFractionDigits: 1 }) + "%";
}
function btcFmt(n) {
  return Number(n || 0).toLocaleString("it-IT", { minimumFractionDigits: 2, maximumFractionDigits: 8 }) + " BTC";
}
function toneClass(tone) {
  return tone === "success" ? "ok" : tone === "danger" ? "bad" : tone === "warning" ? "warn" : "info";
}

async function api(path, opts) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || res.statusText || "Errore");
  return data;
}

function bindSegmented() {
  $$(".segmented").forEach((el) => {
    el.addEventListener("click", (ev) => {
      const btn = ev.target.closest("button[data-value]");
      if (!btn) return;
      const key = el.dataset.key;
      if (!key) return;
      state[key] = btn.dataset.value;
      el.querySelectorAll("button").forEach((b) => b.classList.toggle("on", b === btn));
      if (key === "currency") persistCurrency(state.currency);
      render();
      if (key === "owner") loadNfts();
    });
  });
}

async function persistCurrency(cur) {
  try { await api("/api/settings", { method: "PUT", body: JSON.stringify({ currency: cur }) }); } catch (_) {}
}

function paintChart(id, config) {
  const canvas = document.getElementById(id);
  if (!canvas || typeof Chart === "undefined") return;
  if (charts[id]) charts[id].destroy();
  config.options = config.options || {};
  config.options.responsive = true;
  config.options.maintainAspectRatio = false;
  charts[id] = new Chart(canvas, config);
}

function commonChart() {
  Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
  Chart.defaults.color = "#475569";
}

function renderMetrics(data) {
  const t = data.totals;
  const view = t.view;
  $("#metrics").innerHTML = [
    metric("Io", money(t.io.total_usd), `DeFi ${money(t.io.defi_usd)} + BTC ${money(t.io.btc_usd)}`, "io"),
    metric("Moglie", money(t.moglie.total_usd), `DeFi ${money(t.moglie.defi_usd)} + BTC ${money(t.moglie.btc_usd)}`, "moglie"),
    metric("Combinato", money(t.tutti.total_usd), `DeFi ${money(t.tutti.defi_usd)} + BTC ${money(t.tutti.btc_usd)}`, "tutti"),
    metric("APY DeFi (" + OWNER_LABEL[state.owner] + ")", pct(view.wapy), ""),
    metric("Reddito annuo DeFi", money(view.year_usd), "circa " + money(view.year_usd / 12) + " al mese, a APY fermi"),
  ].join("");
}
function metric(label, value, detail, klass) {
  return `<div class="metric ${klass || ""}"><div class="value">${value}</div><div class="label">${label}</div>${detail ? `<div class="detail">${detail}</div>` : ""}</div>`;
}

function renderAlloc(data) {
  const pos = data.positions.filter((p) => p.protocol !== "Wallet" || p.usd > 0);
  const defi = data.positions.filter((p) => p.protocol !== "Wallet");
  $("#alloc-mode").classList.toggle("hidden", state.owner !== "tutti");
  let parts;
  if (state.owner === "tutti" && state.alloc === "titolare") {
    const io = defi.filter((p) => p.owner === "io").reduce((s, p) => s + p.usd, 0);
    const moglie = defi.filter((p) => p.owner === "moglie").reduce((s, p) => s + p.usd, 0);
    parts = [{ label: "Io", usd: io }, { label: "Moglie", usd: moglie }];
    $("#alloc-title").textContent = "Ripartizione per titolare";
  } else {
    const map = new Map();
    defi.forEach((p) => map.set(p.protocol, (map.get(p.protocol) || 0) + (Number(p.usd) || 0)));
    parts = [...map.entries()].map(([label, usd]) => ({ label, usd }));
    $("#alloc-title").textContent = "Ripartizione per protocollo";
  }
  paintChart("chart-alloc", {
    type: "doughnut",
    data: {
      labels: parts.map((p) => p.label),
      datasets: [{ data: parts.map((p) => p.usd), backgroundColor: PALETTE, borderWidth: 0 }],
    },
    options: {
      plugins: {
        legend: { position: "bottom" },
        tooltip: { callbacks: { label: (c) => ` ${c.label}: ${money(c.raw)}` } },
      },
      cutout: "62%",
    },
  });
}

function renderApy(data) {
  const pos = data.positions.filter((p) => p.protocol !== "Wallet");
  const labels = pos.map((p) => p.protocol + " " + p.asset + (state.owner === "tutti" ? " · " + OWNER_LABEL[p.owner] : ""));
  paintChart("chart-apy", {
    type: "bar",
    data: {
      labels,
      datasets: [
        { label: "APY attuale", data: pos.map((p) => (p.apy == null ? null : p.apy)), backgroundColor: "#0D9488" },
        { label: "Media 30 giorni", data: pos.map((p) => (p.apy30 == null ? null : p.apy30)), backgroundColor: "#2563EB" },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: { legend: { position: "bottom" } },
      scales: { y: { beginAtZero: true, ticks: { callback: (v) => v + "%" } } },
    },
  });
}

function renderHist(data) {
  const days = Number(state.period);
  const cutoff = (data.ts || Math.floor(Date.now() / 1000)) - days * 86400;
  const rawHist = (data.history || []).filter((h) => h.ts >= cutoff);
  const byBucket = new Map();
  rawHist.forEach((h) => {
    const bucket = Math.floor(h.ts / 3600);
    byBucket.set(bucket, h);
  });
  const hist = [...byBucket.values()];
  const unit = state.currency === "EUR" ? "€" : "$";
  const fx = state.currency === "EUR" ? (state.fx || 0.92) : 1;
  const labels = hist.map((h) => {
    const d = new Date(h.ts * 1000);
    return d.toLocaleDateString("it-IT", { day: "numeric", month: "short" });
  });
  const vals = hist.map((h) => (h.defi_usd || 0) * fx);
  const inc = hist.map((h) => (h.year_usd || 0) * fx);
  $("#hist-caption").textContent = `Storico locale degli ultimi ${days} giorni, per ${OWNER_LABEL[state.owner]}. La linea principale è il valore DeFi; quella secondaria è il reddito annuo stimato.`;
  paintChart("chart-hist", {
    type: "line",
    data: {
      labels,
      datasets: [
        { label: "Valore DeFi", data: vals, borderColor: "#0D9488", backgroundColor: "rgba(13,148,136,0.12)", fill: true, tension: 0.3, yAxisID: "y" },
        { label: "Reddito annuo stimato", data: inc, borderColor: "#F59E0B", backgroundColor: "transparent", tension: 0.3, yAxisID: "y1" },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      plugins: { legend: { position: "bottom" } },
      scales: {
        y: { ticks: { callback: (v) => v.toLocaleString("it-IT") + " " + unit } },
        y1: { position: "right", grid: { drawOnChartArea: false }, ticks: { callback: (v) => v.toLocaleString("it-IT") + " " + unit } },
      },
    },
  });
}

function apyDelta(now, avg) {
  if (now == null || avg == null) return "—";
  const d = Math.round((now - avg) * 10) / 10;
  if (d === 0) return "→  0,0";
  if (d > 0) return `<span class="pill ok">↑  +${pct(d)}</span>`;
  return `<span class="pill bad">↓  ${pct(d)}</span>`;
}
function riskBadge(risk) {
  const map = { basso: ["Basso", "ok"], medio: ["Medio", "warn"], alto: ["Alto", "bad"] };
  const [label, cls] = map[risk] || [risk, "muted"];
  return `<span class="pill ${cls}">${label}</span>`;
}

function renderPositions(data) {
  const pos = data.positions;
  const body = $("#pos-body");
  if (!pos.length) {
    body.innerHTML = `<tr><td colspan="7" class="mini">Nessuna posizione in questa vista.</td></tr>`;
    return;
  }
  body.innerHTML = pos.map((p) => `
    <tr>
      <td>${OWNER_LABEL[p.owner] || p.owner}</td>
      <td>${p.protocol}</td>
      <td>${p.asset}</td>
      <td class="num">${p.usd == null ? "n/d" : money(p.usd)}</td>
      <td class="num">${p.protocol !== "Wallet" && p.apy == null ? "non disponibile" : pct(p.apy)}</td>
      <td class="num">${apyDelta(p.apy, p.apy30)}</td>
      <td>${riskBadge(p.risk)}</td>
    </tr>`).join("");
}

function renderSwitches(data) {
  const box = $("#switches");
  const items = data.switches || [];
  if (!items.length) {
    box.innerHTML = `<p class="mini">Nessuno switch conviene in questa vista, a rischio uguale o inferiore e dopo il gas di circa ${money(data.gas_usd || 0)}.</p>
      <p class="mini">Nessuna transazione parte da qui. Se un giorno vorrai farlo, firmi tu sul tuo portafoglio hardware.</p>`;
    return;
  }
  box.innerHTML = items.map((s) => `
    <div class="callout warn">
      <strong>${s.lead}</strong>
      Spostare ${money(s.usd)} da ${s.from} (${pct(s.from_apy)}, rischio ${s.from_risk}) a ${s.to} (${pct(s.to_apy)}, rischio ${s.to_risk}).
      Stesso rischio o inferiore, rendimento più alto anche dopo il gas (${money(s.gas_usd)}): circa ${money(s.net_year_usd)} in più all'anno. È solo una proposta.
    </div>
    <div class="metrics" style="grid-template-columns:repeat(3,1fr)">
      ${metric("APY", pct(s.from_apy) + " → " + pct(s.to_apy))}
      ${metric("Rischio", s.from_risk + " → " + s.to_risk)}
      ${metric("Guadagno dopo il gas", "+" + money(s.net_year_usd), "stima annua · " + OWNER_LABEL[s.owner])}
    </div>
  `).join("") + `<p class="mini">Nessuna transazione parte da qui. Se un giorno vorrai farlo, firmi tu sul tuo portafoglio hardware.</p>`;
}

function renderRows(el, rows) {
  if (!rows.length) {
    el.innerHTML = `<p class="mini">Nessun elemento in questa vista.</p>`;
    return;
  }
  el.innerHTML = rows.map((s) => `
    <div class="rowitem">
      <div><div class="title">${s.title || s.protocol}</div><div class="detail">${s.detail || ""}</div></div>
      <span class="pill ${toneClass(s.tone)}">${s.label || ""}</span>
    </div>`).join("");
}

function renderBtc(data) {
  const node = data.node || {};
  const pill = $("#btc-node-pill");
  pill.textContent = node.connected ? "nodo locale connesso" : (node.configured ? "indicizzatore non collegato" : "indicizzatore non configurato");
  pill.className = "pill " + (node.connected ? "ok" : "warn");
  const price = data.btc_price_usd || 0;
  $("#btc-props").innerHTML = `
    <p><strong>Fonte dati:</strong> ${node.source || "Nodo Bitcoin Umbrel / Electrs (locale)"} ·
    <strong>Stato:</strong> ${node.detail || "—"} ·
    <strong>Prezzo usato:</strong> ${price ? money(price) + " / BTC" : "non disponibile"} ·
    <strong>Vista:</strong> ${OWNER_LABEL[state.owner]}</p>`;
  const wallets = data.btc_wallets || [];
  const btcAmt = wallets.reduce((s, w) => s + (w.btc || 0), 0);
  const btcUsd = btcAmt * price;
  const view = data.totals.view;
  const portUsd = view.total_usd;
  const share = portUsd ? (btcUsd / portUsd) * 100 : 0;
  $("#btc-metrics").innerHTML = [
    metric("Totale Bitcoin (" + OWNER_LABEL[state.owner] + ")", btcFmt(btcAmt)),
    metric("Valore fiat", money(btcUsd)),
    metric("Quota sul portafoglio", pct(share), `DeFi ${money(view.defi_usd)} + Bitcoin ${money(btcUsd)} = ${money(portUsd)}`),
    metric("Bitcoin di Io", btcFmt(data.totals.io.btc), money(data.totals.io.btc_usd)),
    metric("Bitcoin della Moglie", btcFmt(data.totals.moglie.btc), money(data.totals.moglie.btc_usd)),
  ].join("");

  $("#btc-cards").innerHTML = wallets.length ? wallets.map((w) => `
    <div class="card wallet-card">
      <div class="card-head"><h3>${w.label}</h3><span class="pill ${w.connected ? "ok" : "warn"}">${w.status}</span></div>
      <p class="mini">${OWNER_LABEL[w.owner]} · ${w.kind} · ${w.key_masked}</p>
      <div class="value" style="font-size:1.3rem;font-weight:800">${btcFmt(w.btc)}</div>
      <p class="mini">${money((w.btc || 0) * price)}</p>
      <p class="mini">Ultimo movimento: ${w.last || "—"}</p>
    </div>`).join("") : `<p class="mini">Nessun xpub in questa vista. Aggiungilo in Impostazioni.</p>`;

  paintChart("chart-btc", {
    type: "doughnut",
    data: {
      labels: wallets.map((w) => w.label + " · " + OWNER_LABEL[w.owner]),
      datasets: [{ data: wallets.map((w) => w.btc || 0), backgroundColor: PALETTE, borderWidth: 0 }],
    },
    options: { plugins: { legend: { position: "bottom" }, tooltip: { callbacks: { label: (c) => ` ${c.label}: ${btcFmt(c.raw)}` } } }, cutout: "62%" },
  });

  let portParts, portTitle, portTotal;
  if (state.owner === "tutti") {
    portParts = [{ label: "Io", value: data.totals.io.total_usd }, { label: "Moglie", value: data.totals.moglie.total_usd }];
    portTitle = "Portafoglio per titolare";
    portTotal = data.totals.tutti.total_usd;
  } else {
    portParts = [{ label: "Bitcoin", value: btcUsd }, { label: "DeFi", value: view.defi_usd }];
    portTitle = "Bitcoin nel portafoglio";
    portTotal = portUsd;
  }
  $("#port-title").textContent = portTitle;
  paintChart("chart-port", {
    type: "doughnut",
    data: {
      labels: portParts.map((p) => p.label),
      datasets: [{ data: portParts.map((p) => p.value), backgroundColor: PALETTE, borderWidth: 0 }],
    },
    options: { plugins: { legend: { position: "bottom" }, tooltip: { callbacks: { label: (c) => ` ${c.label}: ${money(c.raw)}` } } }, cutout: "62%" },
  });

  const tabs = $("#btc-tabs");
  const opts = [{ id: "all", label: "Tutti" }, ...wallets.map((w) => ({ id: String(w.id), label: w.label }))];
  if (!opts.some((o) => o.id === state.btcTab)) state.btcTab = "all";
  tabs.innerHTML = opts.map((o) => `<button type="button" data-value="${o.id}" class="${state.btcTab === o.id ? "on" : ""}">${o.label}</button>`).join("");
  tabs.onclick = (ev) => {
    const btn = ev.target.closest("button[data-value]");
    if (!btn) return;
    state.btcTab = btn.dataset.value;
    render();
  };
  const moves = (data.btc_moves || []).filter((m) => state.btcTab === "all" || String(m.wallet_id) === state.btcTab);
  $("#btc-moves").innerHTML = moves.length ? moves.map((m) => `
    <tr>
      <td>${OWNER_LABEL[m.owner] || m.owner}</td>
      <td>${m.wallet || ""}</td>
      <td>${m.height || "—"}</td>
      <td><span class="pill ${m.kind === "Ricevuto" ? "ok" : "muted"}">${m.kind}</span></td>
      <td class="num">${(m.kind === "Inviato" ? "−" : "+")}${btcFmt(m.btc || 0)}</td>
    </tr>`).join("") : `<tr><td colspan="5" class="mini">Nessun movimento in questa vista. Se l'indicizzatore non è collegato, i movimenti restano vuoti.</td></tr>`;

  renderRows($("#btc-security"), data.btc_security || []);
}

function moneyOrMissing(usd) {
  if (usd == null || Number.isNaN(Number(usd))) return "prezzo non disponibile";
  return money(usd);
}
function nativeOrMissing(amount, symbol) {
  if (amount == null) return "non disponibile";
  return Number(amount).toLocaleString("it-IT", { maximumFractionDigits: 4 }) + (symbol ? " " + symbol : "");
}
function renderNfts() {
  const box = $("#nft-list");
  const caption = $("#nft-caption");
  if (!box) return;
  const data = state.nfts;
  if (!data) {
    box.innerHTML = `<p class="mini">Caricamento NFT…</p>`;
    return;
  }
  const items = (data.items || []).filter((item) => state.showHiddenNfts || !item.hidden);
  if (caption) {
    caption.textContent = (data.limit_note || "") + " " + (data.listing_note || "");
  }
  const hiddenCount = (data.items || []).filter((item) => item.hidden).length;
  const btn = $("#btn-nft-hidden");
  if (btn) btn.textContent = state.showHiddenNfts ? "Nascondi i filtrati" : `Mostra nascosti (${hiddenCount})`;
  if (!items.length) {
    box.innerHTML = `<p class="mini">${data.errors && data.errors.length ? escapeHtml(data.errors.join(" ")) : "Nessun NFT in questa vista, oppure sono stati filtrati come spam."}</p>`;
    return;
  }
  const groups = new Map();
  items.forEach((item) => {
    const key = state.owner === "tutti" ? item.owner : "vista";
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(item);
  });
  box.innerHTML = [...groups.entries()].map(([key, rows]) => `
    ${key === "vista" ? "" : `<h3>${OWNER_LABEL[key] || escapeHtml(key)}</h3>`}
    <div class="nft-grid">
      ${rows.map((item) => `
        <article class="nft-card ${item.hidden ? "hidden-nft" : ""}">
          ${item.image ? `<img alt="" src="${escapeHtml(item.image)}" />` : `<div class="empty">senza immagine</div>`}
          <h3>${escapeHtml(item.name || "Senza nome")}</h3>
          <p class="mini">${escapeHtml(item.collection || "")} · ${escapeHtml(item.chain_label || item.chain || "")} · ${OWNER_LABEL[item.owner] || ""}</p>
          <p class="mini">Floor: ${nativeOrMissing(item.floor_native, item.native_symbol)} · ${moneyOrMissing(item.floor_usd)}</p>
          <p class="mini">Vendite recenti (media 24h): ${nativeOrMissing(item.recent_sale_native, item.native_symbol)} · ${moneyOrMissing(item.recent_sale_usd)}</p>
          <p class="mini">In vendita: ${item.listed == null ? "non disponibile" : item.listed ? nativeOrMissing(item.list_price_native, item.native_symbol) : "no"}</p>
          <p><strong>Suggerimento:</strong> ${item.suggestion_usd == null && item.suggestion_native == null ? "prezzo non disponibile" : nativeOrMissing(item.suggestion_native, item.native_symbol) + " · " + moneyOrMissing(item.suggestion_usd)}</p>
          <p class="mini">${escapeHtml(item.suggestion_reason || "")}</p>
          ${item.hidden ? `<p class="mini">Nascosto: ${escapeHtml(item.hidden_reason || "")}</p>` : ""}
        </article>
      `).join("")}
    </div>
  `).join("");
}

async function loadNfts() {
  const box = $("#nft-list");
  if (box && !state.nfts) box.innerHTML = `<p class="mini">Caricamento NFT…</p>`;
  try {
    state.nfts = await api("/api/nfts?owner=" + encodeURIComponent(state.owner));
    renderNfts();
  } catch (err) {
    if (box) box.innerHTML = `<p class="mini">${escapeHtml(err.message)}</p>`;
  }
}

function render() {
  const data = state.data;
  if (!data) return;
  state.fx = data.fx_eur || 0.92;
  const hasWallets = (data.positions && data.positions.length) || (data.btc_wallets && data.btc_wallets.length);
  const listed = true;
  $("#empty-state").classList.toggle("hidden", !!(data.totals && (data.totals.tutti.total_usd || data.positions.length || data.btc_wallets.length)));
  const errs = data.errors || [];
  $("#errors").innerHTML = errs.length ? `<div class="errbar">${errs.map((e) => escapeHtml(String(e))).join("<br>")}</div>` : "";
  renderMetrics(data);
  renderAlloc(data);
  renderApy(data);
  renderHist(data);
  renderPositions(data);
  renderSwitches(data);
  renderRows($("#security"), data.security || []);
  renderBtc(data);
}

function escapeHtml(s) {
  return s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

async function load(force) {
  $("#btn-refresh").disabled = true;
  try {
    const data = await api("/api/portfolio?owner=" + encodeURIComponent(state.owner) + (force ? "&refresh=true" : ""));
    state.data = data;
    if (data.fx_eur) state.fx = data.fx_eur;
    render();
    loadNfts();
  } catch (err) {
    $("#errors").innerHTML = `<div class="errbar">${escapeHtml(err.message)}</div>`;
  } finally {
    $("#btn-refresh").disabled = false;
  }
}

function openSettings(open) {
  $("#settings").classList.toggle("hidden", !open);
  $("#settings-backdrop").classList.toggle("hidden", !open);
}

async function loadSettings() {
  const s = await api("/api/settings");
  const form = $("#form-settings");
  form.eth_rpc_url.value = s.eth_rpc_url || "";
  form.electrs_host.value = s.electrs_host || "";
  form.electrs_port.value = s.electrs_port || 50001;
  if (form.nft_api_provider) form.nft_api_provider.value = s.nft_api_provider || "";
  if (form.nft_api_key) form.nft_api_key.value = "";
  const keyLine = $("#nft-key-line");
  if (keyLine) {
    keyLine.textContent = s.nft_api_key_set
      ? `Chiave NFT salvata nel database locale (${s.nft_api_provider || "fonte"}): ${s.nft_api_key_masked}`
      : "Chiave NFT: non impostata. Senza chiave la lista funziona lo stesso e il prezzo può restare non disponibile.";
  }
  form.gas_estimate_usd.value = s.gas_estimate_usd || "";
  form.tor_enabled.value = s.tor_enabled || "";
  $("#token-line").textContent = "Token MCP (mascherato): " + (s.mcp_token_masked || "—") + ". Lo vedi per intero solo quando lo generi di nuovo.";
  if (s.mcp_token) {
    $("#token-line").textContent = "Nuovo token MCP (copialo ora): " + s.mcp_token;
  }
  const wallets = await api("/api/wallets");
  $("#evm-list").innerHTML = (wallets.evm || []).map((w) =>
    `<p class="mini">${escapeHtml(w.label)} · ${OWNER_LABEL[w.owner]} · ${escapeHtml(w.address)} <button class="btn" data-del-evm="${w.id}" type="button">Rimuovi</button></p>`
  ).join("") || `<p class="mini">Nessun indirizzo Ethereum.</p>`;
  $("#btc-list").innerHTML = (wallets.btc || []).map((w) =>
    `<p class="mini">${escapeHtml(w.label)} · ${OWNER_LABEL[w.owner]} · ${escapeHtml(w.kind)} · ${escapeHtml(w.descriptor_masked)} <button class="btn" data-del-btc="${w.id}" type="button">Rimuovi</button></p>`
  ).join("") || `<p class="mini">Nessun conto Bitcoin.</p>`;
}

function formData(form) {
  return Object.fromEntries(new FormData(form).entries());
}
function numOrNull(v) {
  if (v === "" || v == null) return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

async function boot() {
  commonChart();
  bindSegmented();
  $("#btn-refresh").addEventListener("click", () => load(true));
  $("#btn-settings").addEventListener("click", async () => { await loadSettings(); openSettings(true); });
  $("#btn-close-settings").addEventListener("click", () => openSettings(false));
  $("#settings-backdrop").addEventListener("click", () => openSettings(false));
  $("#form-settings").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const f = formData(ev.target);
    const body = {
      eth_rpc_url: f.eth_rpc_url,
      electrs_host: f.electrs_host,
      electrs_port: Number(f.electrs_port || 50001),
      gas_estimate_usd: f.gas_estimate_usd === "" ? 0 : Number(f.gas_estimate_usd),
      tor_enabled: f.tor_enabled === "" ? null : f.tor_enabled === "1",
      nft_api_provider: f.nft_api_provider || "",
    };
    if (f.nft_api_key) body.nft_api_key = f.nft_api_key;
    try {
      await api("/api/settings", { method: "PUT", body: JSON.stringify(body) });
      await load(true);
      await loadSettings();
    } catch (err) { alert(err.message); }
  });
  $("#btn-nft-hidden").addEventListener("click", () => {
    state.showHiddenNfts = !state.showHiddenNfts;
    renderNfts();
  });
  $("#btn-clear-nft-key").addEventListener("click", async () => {
    try {
      await api("/api/settings", { method: "PUT", body: JSON.stringify({ nft_api_key: "", nft_api_provider: "" }) });
      await loadSettings();
    } catch (err) { alert(err.message); }
  });
  $("#btn-export").addEventListener("click", async () => {
    try {
      const data = await api("/api/backup");
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "nodo-defi-monitor-backup.json";
      a.click();
      URL.revokeObjectURL(url);
    } catch (err) { alert(err.message); }
  });
  $("#import-file").addEventListener("change", async (ev) => {
    const file = ev.target.files && ev.target.files[0];
    if (!file) return;
    try {
      const text = await file.text();
      const body = JSON.parse(text);
      const result = await api("/api/backup", { method: "POST", body: JSON.stringify(body) });
      alert(`Importati ${result.evm_added} indirizzi e ${result.btc_added} conti Bitcoin. I portafogli già presenti non sono stati cancellati.`);
      await loadSettings();
      await load(true);
    } catch (err) { alert(err.message); }
    ev.target.value = "";
  });
  $("#btn-rotate").addEventListener("click", async () => {
    try {
      const s = await api("/api/settings", { method: "PUT", body: JSON.stringify({ rotate_token: true }) });
      await loadSettings();
      if (s.mcp_token) alert("Nuovo token MCP (copialo ora):\n" + s.mcp_token);
    } catch (err) { alert(err.message); }
  });
  $("#form-evm").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const f = formData(ev.target);
    try {
      await api("/api/wallets/evm", { method: "POST", body: JSON.stringify(f) });
      ev.target.reset();
      await loadSettings();
      await load(true);
    } catch (err) { alert(err.message); }
  });
  $("#form-btc").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const f = formData(ev.target);
    const body = {
      label: f.label, owner: f.owner, descriptor: f.descriptor, role: f.role,
      alert_out_btc: numOrNull(f.alert_out_btc),
      alert_min_btc: numOrNull(f.alert_min_btc),
      hot_threshold_btc: numOrNull(f.hot_threshold_btc),
    };
    try {
      await api("/api/wallets/btc", { method: "POST", body: JSON.stringify(body) });
      ev.target.reset();
      await loadSettings();
      await load(true);
    } catch (err) { alert(err.message); }
  });
  document.addEventListener("click", async (ev) => {
    const evm = ev.target.closest("[data-del-evm]");
    const btc = ev.target.closest("[data-del-btc]");
    try {
      if (evm) { await api("/api/wallets/evm/" + evm.dataset.delEvm, { method: "DELETE" }); await loadSettings(); await load(true); }
      if (btc) { await api("/api/wallets/btc/" + btc.dataset.delBtc, { method: "DELETE" }); await loadSettings(); await load(true); }
    } catch (err) { alert(err.message); }
  });
  await load(true);
}

boot();
