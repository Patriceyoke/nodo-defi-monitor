/* Monitor DeFi frontend — original, Italian, read-only. */
const PALETTE = ["#0D9488", "#2563EB", "#F59E0B", "#7C3AED", "#06B6D4", "#E11D48", "#64748B"];
const OWNER_LABEL = { io: "Io", moglie: "Moglie", tutti: "Combinato" };
const state = { owner: "tutti", period: "30", currency: "USD", alloc: "protocollo", btcTab: "all", data: null, fx: null, nfts: null, showHiddenNfts: false, showHiddenApprovals: false, showHiddenSwitches: false, refreshing: false, stale: true, positionsOpenApplied: false };
let loadGen = 0;
let applyingPositions = false;
const charts = {};

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => [...document.querySelectorAll(sel)];

function formatAmount(n) {
  const abs = Math.abs(n);
  const digits = abs >= 100 || abs === 0 ? 0 : 2;
  return n.toLocaleString("th-TH", { minimumFractionDigits: 0, maximumFractionDigits: digits });
}
function money(usd, cur = state.currency) {
  const n0 = Number(usd);
  if (!Number.isFinite(n0)) return "n/d";
  if (cur === "THB") {
    if (!state.fx) return "n/d";
    return "฿" + formatAmount(n0 * state.fx);
  }
  return formatAmount(n0) + " $";
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

async function api(path, opts = {}) {
  const headers = { ...(opts.headers || {}) };
  if (opts.body) headers["Content-Type"] = "application/json";
  const res = await fetch(path, { ...opts, headers });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = data && data.detail;
    const message = Array.isArray(detail)
      ? detail.map((item) => item.msg || JSON.stringify(item)).join("; ")
      : (detail || data.errors && data.errors.join("; ") || res.statusText || "Errore di rete");
    throw new Error(typeof message === "string" ? message : "Errore di rete");
  }
  return data;
}

function showBanner(list, ensAlerts) {
  const box = $("#errors");
  if (!box) return;
  const errs = (list || []).map((item) => String(item || "")).filter(Boolean);
  const ensHtml = (ensAlerts || []).map((item) => {
    const tone = item.level === "danger" ? "bad" : "warn";
    const who = OWNER_LABEL[item.owner] || item.label || "";
    return `<div class="callout ${tone}"><strong>${escapeHtml(item.name || "ENS")}</strong> ${escapeHtml(item.text || "")}${who ? " · " + escapeHtml(who) : ""}</div>`;
  }).join("");
  const errHtml = errs.length ? `<div class="errbar">${errs.map((e) => escapeHtml(e)).join("<br>")}</div>` : "";
  box.innerHTML = ensHtml + errHtml;
}

function securityLine(summary) {
  const approvals = Number(summary.approvals || 0);
  const other = Number(summary.incidents || 0) + Number(summary.outflows || 0);
  const approvazioni = approvals === 1 ? "1 approvazione" : `${approvals} approvazioni`;
  const avvisi = other === 1 ? "1 avviso" : `${other} avvisi`;
  if (approvals && other) return `${approvazioni} e ${avvisi} da controllare`;
  if (approvals) return `${approvazioni} da controllare`;
  return `${avvisi} da controllare`;
}

function liveSecuritySummary(fallback) {
  const data = state.security;
  if (data && data.ready) {
    const approvals = securityRows(data.approvals).filter((row) => !row.hidden);
    const outflows = securityRows(data.outflows);
    const incidents = (data.incidents || []).filter((row) => state.owner === "tutti" || (row.owners || []).includes(state.owner));
    return { approvals: approvals.length, incidents: incidents.length, outflows: outflows.length, visible: approvals.length + incidents.length + outflows.length };
  }
  const pack = fallback || {};
  if (pack[state.owner]) return pack[state.owner];
  return pack.view || pack;
}

function renderSecuritySummary(summary) {
  const box = $("#security-summary");
  if (!box) return;
  const visible = summary && Number(summary.visible || 0);
  if (!visible) {
    box.innerHTML = "";
    return;
  }
  box.innerHTML = `<div class="callout warn"><a href="#wallet-security" data-goto-security>${escapeHtml(securityLine(summary))} · Vai a Sicurezza</a></div>`;
}

function updatedClock(ts) {
  if (!ts) return "";
  const date = new Date(Number(ts) * 1000);
  if (Number.isNaN(date.getTime())) return "";
  const time = date.toLocaleTimeString("it-IT", { hour: "2-digit", minute: "2-digit" });
  const now = new Date();
  const sameDay = date.getFullYear() === now.getFullYear()
    && date.getMonth() === now.getMonth()
    && date.getDate() === now.getDate();
  if (sameDay) return time;
  const day = date.toLocaleDateString("it-IT", { day: "2-digit", month: "2-digit" });
  return day + " " + time;
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
      if (key === "owner") {
        state.nfts = null;
        const details = $("#nft-details");
        if (details && details.open) loadNfts(false);
        const sec = $("#security-details");
        if (sec && sec.open) renderSecurity();
      }
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
  ].join("");
  renderIncome(data);
}
function renderIncome(data) {
  const box = $("#income-cards");
  const body = $("#income-body");
  if (!box || !body) return;
  const inc = data.income;
  if (!inc) {
    box.innerHTML = "";
    body.innerHTML = "";
    return;
  }
  box.innerHTML = ["io", "moglie", "tutti"].map((key) => {
    const row = inc[key] || {};
    const selected = state.owner === key ? " selected" : "";
    return `<div class="metric${selected}">
      <div class="label">Reddito · ${OWNER_LABEL[key]}</div>
      <div class="value">${money(row.month_usd)}</div>
      <div class="detail">al mese · settimana ${money(row.week_usd)} · anno ${money(row.year_usd)}</div>
      <div class="detail">${pct(row.apy_month)} al mese · ${pct(row.apy_year)} annuo</div>
      <div class="detail">Bitcoin ${btcFmt((data.totals[key] || {}).btc)} · ${money((data.totals[key] || {}).btc_usd)} · non fruttifero</div>
    </div>`;
  }).join("");
  const table = inc.by_protocol || {};
  const rows = Array.isArray(table) ? table : (table[state.owner] || []);
  body.innerHTML = rows.length ? rows.map((row) => `
    <tr>
      <td>${escapeHtml(row.protocol)}</td>
      <td class="num">${money(row.usd)}</td>
      <td class="num">${pct(row.apy_month)}</td>
      <td class="num">${pct(row.apy_year)}</td>
      <td class="num">${money(row.week_usd)}</td>
      <td class="num">${money(row.month_usd)}</td>
      <td class="num">${money(row.year_usd)}</td>
    </tr>
  `).join("") : `<tr><td colspan="7" class="mini">Nessun reddito stimato in questa vista. Serve un APY sulla posizione.</td></tr>`;
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
  const totals = data.totals || {};
  const apyOf = (owner, field) => {
    const value = totals[owner] && totals[owner][field];
    return value == null ? null : Number(value);
  };
  const series = [
    { key: "io", color: "#0D9488", border: "#0D9488", width: 0 },
    { key: "moglie", color: "#7C3AED", border: "#7C3AED", width: 0 },
    { key: "tutti", color: "#B45309", border: "#111827", width: 3 },
  ];
  paintChart("chart-apy-owners", {
    type: "bar",
    data: {
      labels: ["APY attuale", "Media 30 giorni"],
      datasets: series.map((item) => ({
        label: OWNER_LABEL[item.key],
        data: [apyOf(item.key, "wapy"), apyOf(item.key, "wapy30")],
        backgroundColor: item.color,
        borderColor: item.border,
        borderWidth: item.width,
      })),
    },
    options: {
      plugins: {
        legend: { position: "bottom" },
        tooltip: {
          callbacks: {
            label: (c) => ` ${c.dataset.label}: ${Number(c.raw).toLocaleString("it-IT", { maximumFractionDigits: 2 })}%`,
          },
        },
      },
      scales: { y: { beginAtZero: true, ticks: { callback: (v) => v + "%" } } },
    },
  });
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
  const unit = state.currency === "THB" ? "฿" : "$";
  const fx = state.currency === "THB" ? (state.fx || 0) : 1;
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
  const summary = $("#pos-summary");
  const pack = data.position_summary || {};
  if (summary) {
    const io = pack.io || {};
    const moglie = pack.moglie || {};
    const count = (io.count || 0) + (moglie.count || 0);
    summary.textContent = count
      ? `${count} posizioni · Io ${money(io.usd || 0)} · Moglie ${money(moglie.usd || 0)}`
      : "Nessuna posizione";
  }
  const details = $("#positions-details");
  if (details && !state.positionsOpenApplied && typeof data.positions_open === "boolean") {
    applyingPositions = true;
    details.open = data.positions_open;
    applyingPositions = false;
    state.positionsOpenApplied = true;
  }
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

const YIELD_HOSTS = new Set([
  "app.aave.com", "app.compound.xyz", "app.spark.finance", "fluid.io",
  "stake.lido.fi", "app.sky.money", "stake.rocketpool.net", "maple.finance",
]);
function yieldLink(url) {
  let parsed;
  try { parsed = new URL(String(url || "")); } catch (err) { return ""; }
  if (parsed.protocol !== "https:" || !YIELD_HOSTS.has(parsed.hostname)) return "";
  return `<a href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer">Vai alla yield</a>`;
}
function renderSwitches(data) {
  const box = $("#switches");
  const all = data.switches || [];
  const hidden = all.filter((item) => item.hidden);
  const items = state.showHiddenSwitches ? all : all.filter((item) => !item.hidden);
  const toggle = $("#btn-switch-hidden");
  if (toggle) toggle.textContent = state.showHiddenSwitches ? "Nascondi i nascosti" : `Mostra nascosti (${hidden.length})`;
  if (!items.length) {
    const note = hidden.length && !state.showHiddenSwitches ? " Quelli nascosti tornano se il vantaggio cresce di almeno un punto, se cambia la destinazione, o dopo 7 giorni." : "";
    box.innerHTML = `<p class="mini">Nessuno switch conviene in questa vista, a rischio uguale o inferiore e dopo il gas di circa ${money(data.gas_usd || 0)}.${note}</p>
      <p class="mini">Nessuna transazione parte da qui. Se un giorno vorrai farlo, firmi tu sul tuo portafoglio hardware.</p>`;
    return;
  }
  box.innerHTML = items.map((s) => {
    const unstable = s.unstable
      ? `<p class="mini warn-text">APY instabile: il guadagno usa la media a 30 giorni (${pct(s.from_rate)} → ${pct(s.to_rate)}), non il picco.</p>`
      : "";
    const source = `<p class="mini">Fonte APY: ${escapeHtml(s.apy_source || "DefiLlama")}${s.apy_at ? " · " + updatedClock(s.apy_at) : ""}</p>`;
    return `
    <div class="callout warn">
      <strong>${escapeHtml(s.lead)}</strong>
      <div class="metrics compact">
        ${metric("APY", pct(s.from_apy) + " → " + pct(s.to_apy))}
        ${metric("Rischio", escapeHtml(s.from_risk) + " → " + escapeHtml(s.to_risk))}
        ${metric("Guadagno dopo il gas", "+" + money(s.net_year_usd), "stima annua · " + OWNER_LABEL[s.owner])}
      </div>
      <p>Spostare ${money(s.usd)} da ${escapeHtml(s.from)} (${pct(s.from_apy)}, rischio ${escapeHtml(s.from_risk)}) a ${escapeHtml(s.to)} (${pct(s.to_apy)}, rischio ${escapeHtml(s.to_risk)}).
      Stesso rischio o inferiore, rendimento più alto anche dopo il gas (${money(s.gas_usd)}): circa ${money(s.net_year_usd)} in più all'anno. È solo una proposta.</p>
      ${unstable}
      ${source}
      <p>${yieldLink(s.url)} <button class="btn" type="button" data-switch-${s.hidden ? "unhide" : "hide"} data-owner="${escapeHtml(s.owner || "")}" data-from="${escapeHtml(s.from || "")}" data-to="${escapeHtml(s.to || "")}" data-spread="${escapeHtml(s.spread)}">${s.hidden ? "Ripristina" : "Nascondi"}</button></p>
    </div>`;
  }).join("") + `<p class="mini">Nessuna transazione parte da qui. Se un giorno vorrai farlo, firmi tu sul tuo portafoglio hardware.</p>`;
}

function electrsDiagnostic(row) {
  const attempts = row.attempts || [];
  const net = row.network || {};
  if (!attempts.length && !(net.ips || net.networks || net.gateway)) return "";
  const lines = attempts.map((item) => {
    const result = item.ok ? "TCP e server.version ok" : (item.error || "errore");
    const elapsed = item.ms != null ? ` · ${item.ms} ms` : "";
    return `<li><code>${escapeHtml(item.host)}:${escapeHtml(item.port)}</code> — ${escapeHtml(result)}${elapsed}</li>`;
  }).join("");
  const steps = (row.steps || []).map((step) => {
    const outcome = step.ok ? (step.detail || "ok") : (step.detail || step.error || "errore");
    const elapsed = step.ms != null ? `${step.ms} ms` : "n/d";
    return `<li>${escapeHtml(step.name)} — ${escapeHtml(outcome)} · ${escapeHtml(elapsed)}</li>`;
  }).join("");
  const ips = (net.ips || []).join(", ") || "n/d";
  const networks = (net.networks || []).join(", ") || "n/d";
  const gateway = net.gateway || "n/d";
  const domain = net.domain ? ` · Dominio: ${escapeHtml(net.domain)}` : "";
  const injected = net.electrs_ip ? ` · APP_ELECTRS_NODE_IP: ${escapeHtml(net.electrs_ip)}:${escapeHtml(net.electrs_port || "")}` : "";
  return `<details class="electrs-diag"><summary>Diagnostica Electrs</summary>
    <p class="mini">IP del contenitore: ${escapeHtml(ips)} · Reti: ${escapeHtml(networks)} · Gateway: ${escapeHtml(gateway)}${domain}${injected}</p>
    <ul>${lines}</ul>
    ${steps ? `<p class="mini">Passi sul server scelto</p><ul>${steps}</ul>` : ""}
    </details>`;
}
function renderRows(el, rows) {
  if (!rows.length) {
    el.innerHTML = `<p class="mini">Nessun elemento in questa vista.</p>`;
    return;
  }
  el.innerHTML = rows.map((s) => `
    <div class="rowitem">
      <div><div class="title">${escapeHtml(s.title || s.protocol)}</div><div class="detail">${escapeHtml(s.detail || "")}${electrsDiagnostic(s)}</div></div>
      <span class="pill ${toneClass(s.tone)}">${escapeHtml(s.label || "")}</span>
    </div>`).join("");
}

function btcProgressLine(wallet) {
  const rows = (state.btcProgress && state.btcProgress.wallets) || [];
  const row = rows.find((item) => item.id === wallet.id);
  if (!row || row.status !== "In scansione") return "";
  const branch = row.branch === "change" ? "Resto" : row.branch === "receive" ? "Ricezione" : "Avvio";
  return `<p class="mini">Scansione ${branch} · indice ${row.index ?? 0} · ${row.checked ?? 0} indirizzi</p>`;
}
function branchLine(label, row) {
  if (!row) return "";
  const last = row.last_used == null ? "nessuno" : "#" + row.last_used;
  return `${label}: ${row.scanned || 0} indirizzi, ultimo usato ${last}`;
}
function scanDetail(scan) {
  if (!scan) return "";
  const confirmed = btcFmt((scan.confirmed_sats || 0) / 100000000);
  const unconfirmed = btcFmt((scan.unconfirmed_sats || 0) / 100000000);
  return `<details class="electrs-diag"><summary>Dettaglio scansione</summary>
    <p class="mini">${escapeHtml(branchLine("Ricezione", scan.receive))}</p>
    <p class="mini">${escapeHtml(branchLine("Resto", scan.change))}</p>
    <p class="mini">${scan.utxo_count || 0} UTXO · confermati ${confirmed} · non confermati ${unconfirmed}</p>
  </details>`;
}
function renderBtc(data) {
  const node = data.node || {};
  const pill = $("#btc-node-pill");
  pill.textContent = node.indexing
    ? "Electrs sta indicizzando"
    : node.connected
      ? "nodo locale connesso"
      : node.configured
        ? "Electrs non raggiungibile"
        : "indicizzatore non configurato";
  pill.className = "pill " + (node.connected && !node.indexing ? "ok" : "warn");
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
      <p class="mini">${money(w.usd != null ? w.usd : (w.btc || 0) * price)}${w.utxo_count != null ? " · " + w.utxo_count + " UTXO" : ""}</p>
      <p class="mini">Ultimo movimento: ${escapeHtml(w.last || "—")}</p>
      ${btcProgressLine(w)}
      ${w.stale && w.scanned_at ? `<p class="mini warn-text">dati del ${updatedClock(w.scanned_at)}</p>` : ""}
      ${scanDetail(w.scan)}
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
function nftActions(item) {
  const attrs = `data-chain="${escapeHtml(item.chain || "")}" data-contract="${escapeHtml(item.contract || "")}" data-token="${escapeHtml(item.token_id || "")}"`;
  if (item.user_hidden) {
    const kind = item.hide_kind === "collection" ? "collection" : "token";
    return `<button class="btn" type="button" data-nft-unhide data-kind="${kind}" ${attrs}>Ripristina</button>`;
  }
  return `<button class="btn" type="button" data-nft-hide data-kind="token" ${attrs}>Nascondi</button>
    <button class="btn ghost" type="button" data-nft-hide data-kind="collection" ${attrs}>Nascondi collezione</button>`;
}
function nftCard(item) {
  const sale = item.liquidity ? `<p class="mini">Posizione di liquidità: non è un NFT da vendere.</p>` : `
    <p class="mini">Floor: ${nativeOrMissing(item.floor_native, item.native_symbol)} · ${moneyOrMissing(item.floor_usd)}</p>
    <p class="mini">Vendite recenti (media 24h): ${nativeOrMissing(item.recent_sale_native, item.native_symbol)} · ${moneyOrMissing(item.recent_sale_usd)}</p>
    <p class="mini">In vendita: ${item.listed == null ? "non disponibile" : item.listed ? nativeOrMissing(item.list_price_native, item.native_symbol) : "no"}</p>
    <p><strong>Suggerimento:</strong> ${item.suggestion_usd == null && item.suggestion_native == null ? "prezzo non disponibile" : nativeOrMissing(item.suggestion_native, item.native_symbol) + " · " + moneyOrMissing(item.suggestion_usd)}</p>
    <p class="mini">${escapeHtml(item.suggestion_reason || "")}</p>`;
  const ens = item.ens && item.ens.expiry ? (() => {
    const when = new Date(item.ens.expiry * 1000).toLocaleDateString("it-IT");
    const grace = item.ens.grace_ended ? "grazia terminata" : item.ens.in_grace ? "in periodo di grazia" : `${item.ens.days_left} giorni`;
    const tone = item.ens.level === "danger" ? "bad" : item.ens.level === "warning" ? "warn" : "ok";
    return `<p class="mini">Scadenza .eth: ${escapeHtml(when)} · <span class="pill ${tone}">${escapeHtml(grace)}</span></p>`;
  })() : "";
  return `<article class="nft-card ${item.hidden ? "hidden-nft" : ""}">
    ${item.image ? `<img alt="" src="${escapeHtml(item.image)}" />` : `<div class="empty">senza immagine</div>`}
    <h3>${escapeHtml(item.name || "Senza nome")}</h3>
    <p class="mini">${escapeHtml(item.collection || "")} · ${escapeHtml(item.chain_label || item.chain || "")} · ${OWNER_LABEL[item.owner] || ""}</p>
    ${ens}
    ${sale}
    ${item.hidden ? `<p class="mini">Nascosto: ${escapeHtml(item.hidden_reason || "")}</p>` : ""}
    <p>${nftActions(item)}</p>
  </article>`;
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
  const all = data.items || [];
  const hiddenCount = all.filter((item) => item.hidden).length;
  const btn = $("#btn-nft-hidden");
  if (btn) btn.textContent = state.showHiddenNfts ? "Nascondi i filtrati" : `Mostra nascosti (${hiddenCount})`;
  if (caption) caption.textContent = (data.limit_note || "") + " " + (data.listing_note || "");
  const status = $("#nft-status");
  if (status) {
    const clock = updatedClock(data.cached_at);
    status.textContent = data.from_cache && clock ? `Dati NFT in cache, aggiornati alle ${clock}.` : clock ? `NFT aggiornati alle ${clock}.` : "";
  }
  const collectibles = all.filter((item) => !item.liquidity && (state.showHiddenNfts || !item.hidden));
  const liquidity = all.filter((item) => item.liquidity && !item.user_hidden);
  const groups = new Map();
  collectibles.forEach((item) => {
    const key = state.owner === "tutti" ? item.owner : "vista";
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(item);
  });
  const grid = collectibles.length ? [...groups.entries()].map(([key, rows]) => `
    ${key === "vista" ? "" : `<h3>${OWNER_LABEL[key] || escapeHtml(key)}</h3>`}
    <div class="nft-grid">${rows.map(nftCard).join("")}</div>
  `).join("") : `<p class="mini">${data.errors && data.errors.length ? escapeHtml(data.errors.join(" ")) : "Nessun NFT da collezione in questa vista."}</p>`;
  const lp = liquidity.length ? `<details class="lp-fold">
    <summary>Posizioni di liquidità (${liquidity.length})</summary>
    <p class="mini">Uniswap, Pancake, Aerodrome e simili: sono posizioni, non pezzi da vendere. Nessun prezzo suggerito.</p>
    <div class="nft-grid">${liquidity.map(nftCard).join("")}</div>
  </details>` : "";
  box.innerHTML = grid + lp;
}

function securityRows(rows) {
  return (rows || []).filter((row) => state.owner === "tutti" || row.owner === state.owner);
}
function renderSecurity() {
  const box = $("#sec-list");
  const status = $("#sec-status");
  if (!box) return;
  const data = state.security;
  if (!data) {
    box.innerHTML = `<p class="mini">Apri la sezione per vedere l'ultimo controllo.</p>`;
    return;
  }
  if (status) {
    const clock = updatedClock(data.scanned_at);
    const counts = data.counts || {};
    status.textContent = data.running
      ? "Controllo in corso…"
      : (clock ? `Controllato alle ${clock}. ` : "") + `Avvisi: Io ${counts.io || 0}, Moglie ${counts.moglie || 0}, combinato ${counts.tutti || 0}.`;
  }
  if (!data.ready) {
    box.innerHTML = `<p class="mini">${escapeHtml(data.note || "Nessun controllo ancora.")}</p>`;
    return;
  }
  const allApprovals = securityRows(data.approvals);
  const hiddenApprovals = allApprovals.filter((row) => row.hidden);
  const approvals = state.showHiddenApprovals ? allApprovals : allApprovals.filter((row) => !row.hidden);
  const hiddenBtn = $("#btn-sec-hidden");
  if (hiddenBtn) hiddenBtn.textContent = state.showHiddenApprovals ? "Nascondi le nascoste" : `Mostra nascoste (${hiddenApprovals.length})`;
  const outflows = securityRows(data.outflows);
  const incidents = (data.incidents || []).filter((row) => state.owner === "tutti" || (row.owners || []).includes(state.owner));
  const flagText = (flags) => (flags || []).map((flag) => ({
    unlimited: "illimitata", eoa: "indirizzo semplice", unverified: "non verificato", unknown: "spender sconosciuto", stale: "oltre 180 giorni", exploited: "spender segnalato",
  }[flag] || flag)).join(", ");
  const approvalHtml = approvals.length ? approvals.map((row) => `
    <article class="nft-card">
      <h3>${escapeHtml(row.symbol || "Token")} · ${escapeHtml(row.chain || "")}</h3>
      <p class="mini">${escapeHtml(row.label || "")} · ${OWNER_LABEL[row.owner] || ""} · ${escapeHtml(flagText(row.flags) || "nessun segnale")}</p>
      <p class="mini">A rischio: ${row.value_at_risk_usd == null ? "n/d" : money(row.value_at_risk_usd)}</p>
      <p><a href="${escapeHtml(row.revoke_url || "#")}" target="_blank" rel="noopener">Apri su revoke.cash</a> <span class="mini">La firma resta tua, per esempio su OneKey.</span></p>
      <p><button class="btn" type="button" data-approval-${row.hidden ? "unhide" : "hide"} data-wallet="${escapeHtml(row.wallet || "")}" data-chain="${escapeHtml(row.chain || "")}" data-token="${escapeHtml(row.token || "")}" data-spender="${escapeHtml(row.spender || "")}" data-allowance="${escapeHtml(row.allowance)}">${row.hidden ? "Ripristina" : "Nascondi"}</button></p>
    </article>`).join("") : `<p class="mini">Nessuna approvazione segnalata in questa vista.</p>`;
  const incidentHtml = incidents.length ? incidents.map((row) => `
    <p class="mini"><span class="pill ${row.severity === "danger" ? "bad" : "warn"}">${escapeHtml(row.protocol || "")}</span> ${escapeHtml(row.detail || row.name || "")}</p>
  `).join("") : `<p class="mini">Nessun incidente recente sui protocolli in possesso.</p>`;
  const outflowHtml = outflows.length ? outflows.map((row) => `
    <p class="mini">${escapeHtml(row.symbol || "")} · ${OWNER_LABEL[row.owner] || ""} · da ${escapeHtml(row.previous)} a ${escapeHtml(row.amount)}
      ${row.tx_url ? `<a href="${escapeHtml(row.tx_url)}" target="_blank" rel="noopener">transazione</a>` : ""}</p>
  `).join("") : `<p class="mini">Nessuna uscita sopra la soglia dall'ultimo controllo.</p>`;
  const windowNote = data.partial ? `<p class="mini">La finestra dei log è limitata: un'approvazione molto vecchia può non comparire.</p>` : "";
  box.innerHTML = `<h3>Approvazioni</h3>${approvalHtml}<h3>Protocolli</h3>${incidentHtml}<h3>Uscite</h3>${outflowHtml}${windowNote}<p class="mini">${escapeHtml(data.note || "")}</p>`;
}
async function loadSecurity(force) {
  const box = $("#sec-list");
  const status = $("#sec-status");
  if (state.security && !force) {
    renderSecurity();
    return;
  }
  if (status) status.textContent = force ? "Controllo in corso…" : "Caricamento…";
  if (box && force) box.innerHTML = `<p class="mini">Caricamento controlli…</p>`;
  try {
    if (force) await api("/api/security/refresh", { method: "POST" });
    let data = await api("/api/security?owner=tutti");
    for (let i = 0; i < 40 && data.running; i += 1) {
      await new Promise((resolve) => setTimeout(resolve, 2000));
      data = await api("/api/security?owner=tutti");
    }
    state.security = data;
    if (state.data) state.data.security_summary = data.summary || state.data.security_summary;
    renderSecuritySummary(data.summary);
    renderSecurity();
  } catch (err) {
    if (status) status.textContent = "";
    if (box) box.innerHTML = `<p class="mini">${escapeHtml(err.message)}</p>`;
  }
}
async function loadNfts(force) {
  const box = $("#nft-list");
  const status = $("#nft-status");
  if (state.nfts && !force) {
    renderNfts();
    return;
  }
  if (box) box.innerHTML = `<p class="mini">Caricamento NFT…</p>`;
  if (status) status.textContent = "Caricamento NFT…";
  const query = "/api/nfts?owner=" + encodeURIComponent(state.owner) + (force ? "&refresh=true" : "");
  try {
    state.nfts = await api(query);
    renderNfts();
  } catch (err) {
    if (status) status.textContent = "";
    if (box) box.innerHTML = `<p class="mini">${escapeHtml(err.message)}</p>`;
  }
}

function keepPreviousSnapshot(previous, next) {
  if (!previous || !totalsReady(previous)) return false;
  if (!totalsReady(next)) return true;
  const had = (previous.positions || []).length || Number(previous.totals.tutti.total_usd) || 0;
  const got = (next.positions || []).length || Number(next.totals.tutti.total_usd) || 0;
  return !!(had && !got && (next.saved === false || next.kept_previous));
}
function totalsReady(data) {
  const t = data && data.totals;
  return !!(t && t.io && t.moglie && t.tutti && t.view);
}

function render() {
  const data = state.data;
  if (!data) return;
  if (!totalsReady(data)) {
    showBanner([...(data.errors || []), "Risposta incompleta dal ricalcolo: i totali mancano."]);
    return;
  }
  data.positions = data.positions || [];
  data.btc_wallets = data.btc_wallets || [];
  data.errors = data.errors || [];
  data.history = data.history || [];
  data.security = data.security || [];
  data.btc_security = data.btc_security || [];
  try {
    if (data.currency === "USD" || data.currency === "THB") state.currency = data.currency;
    state.fx = data.fx_thb || null;
    document.querySelectorAll('[data-key="currency"] button').forEach((button) => {
      button.classList.toggle("on", button.dataset.value === state.currency);
    });
    $("#empty-state").classList.toggle("hidden", !!(data.totals.tutti.total_usd || data.positions.length || data.btc_wallets.length || data.wallet_count));
    renderMetrics(data);
    renderAlloc(data);
    renderApy(data);
    renderHist(data);
    renderPositions(data);
    renderSwitches(data);
    const ensRows = (data.ens_alerts || []).map((item) => ({
      title: (item.name || "ENS") + (OWNER_LABEL[item.owner] ? " · " + OWNER_LABEL[item.owner] : ""),
      detail: item.text || "",
      tone: item.level === "danger" ? "danger" : "warning",
      label: item.level === "danger" ? "scaduto" : "in scadenza",
    }));
    renderRows($("#security"), ensRows.concat(data.security));
    renderBtc(data);
    const banners = [...data.errors];
    if ((data.wallet_count || 0) > 0 && !data.positions.length && !data.btc_wallets.length && !state.refreshing) {
      banners.unshift("I portafogli sono salvati, ma questo ricalcolo non ha prodotto posizioni.");
    }
    showBanner(banners, data.ens_alerts || []);
    renderSecuritySummary(liveSecuritySummary(data.security_summary));
    paintFreshness(data);
  } catch (err) {
    showBanner([...(data.errors || []), "Errore nel disegno della pagina: " + (err && err.message ? err.message : err)]);
  }
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

function paintFreshness(data) {
  const pill = $("#source-pill");
  const clock = data && data.saved !== false ? updatedClock(data.ts) : "";
  if (pill) {
    if (!clock) {
      pill.className = "pill muted";
      pill.textContent = "in sola lettura";
    } else if (state.refreshing || state.stale) {
      pill.className = "pill warn";
      pill.textContent = state.refreshing ? `dati del ${clock} · aggiornamento…` : `dati del ${clock}`;
    } else {
      pill.className = "pill muted";
      pill.textContent = `aggiornato alle ${clock}`;
    }
  }
  const line = $("#source-line");
  if (!line) return;
  const sources = (data && data.sources) || {};
  const parts = Object.values(sources).map((item) => {
    const when = item.at ? updatedClock(item.at) : "n/d";
    const failed = !item.ok;
    const text = `${item.label || ""} ${when}${failed ? " · non letto" : ""}`.trim();
    return failed ? `<span class="warn-text">${escapeHtml(text)}</span>` : escapeHtml(text);
  });
  line.innerHTML = parts.join(" · ");
}

async function load(force, extra) {
  const gen = ++loadGen;
  const full = !!(extra && extra.full);
  const button = $("#btn-refresh");
  if (force && button) button.disabled = true;
  if (force) state.refreshing = true;
  else state.stale = true;
  if (state.data) render();
  const owner = encodeURIComponent(state.owner);
  const path = force
    ? "/api/refresh?owner=" + owner + (full ? "&full=1" : "")
    : "/api/portfolio?owner=" + owner;
  const opts = force ? { method: "POST" } : {};
  try {
    const data = await api(path, opts);
    if (gen !== loadGen) return;
    if (keepPreviousSnapshot(state.data, data)) {
      state.refreshing = false;
      state.stale = true;
      showBanner([...(data.errors || []), "Ricalcolo non riuscito: resta l'ultimo saldo."]);
      paintFreshness(state.data);
      return;
    }
    state.data = data;
    state.refreshing = false;
    state.stale = !force || !!data.kept_previous;
    if (data.btc_progress) state.btcProgress = data.btc_progress;
    if (data.fx_thb) state.fx = data.fx_thb;
    render();
    if (data.btc_progress) watchBtc(data.btc_progress);
    if (force) {
      const nftOpen = $("#nft-details") && $("#nft-details").open;
      const secOpen = $("#security-details") && $("#security-details").open;
      if (full || nftOpen) {
        state.nfts = null;
        loadNfts(true);
      }
      if (full || secOpen) {
        state.security = null;
        loadSecurity(true);
      }
    }
  } catch (err) {
    if (gen !== loadGen) return;
    state.refreshing = false;
    state.stale = true;
    showBanner([err && err.message ? err.message : "Ricalcolo non riuscito."]);
    if (state.data) render();
  } finally {
    if (gen !== loadGen) return;
    if (button) button.disabled = false;
  }
}

let btcWatch = 0;
async function watchBtc(progress) {
  state.btcProgress = progress;
  if (!progress || !progress.running) return;
  const watch = ++btcWatch;
  while (watch === btcWatch) {
    await new Promise((resolve) => setTimeout(resolve, 1000));
    if (watch !== btcWatch) return;
    try {
      const next = await api("/api/btc/progress");
      state.btcProgress = next;
      if (state.data) renderBtc(state.data);
      if (!next.running) {
        const owner = encodeURIComponent(state.owner);
        const fresh = await api("/api/portfolio?owner=" + owner);
        if (state.data && fresh) {
          state.data.btc_wallets = fresh.btc_wallets || [];
          state.data.totals = fresh.totals || state.data.totals;
          state.data.btc_moves = fresh.btc_moves || state.data.btc_moves;
          state.data.node = fresh.node || state.data.node;
          state.data.btc_security = fresh.btc_security || state.data.btc_security;
          render();
        }
        return;
      }
    } catch (err) {
      return;
    }
  }
}

function openSettings(open) {
  $("#settings").classList.toggle("hidden", !open);
  $("#settings-backdrop").classList.toggle("hidden", !open);
}

function electrsHint(host) {
  const name = String(host || "").trim().toLowerCase().replace(/\.$/, "");
  if (name === "umbrel.local" || (name.endsWith(".local") && name)) {
    return "Questo nome .local non si risolve dentro l'app. Su Umbrel l'host è electrs_electrs_1 e la porta 50001. Puoi lasciarlo così: l'app prova da sola i contenitori Electrs e Fulcrum.";
  }
  return "";
}
function showElectrsHint(serverText) {
  const el = $("#electrs-hint");
  const form = $("#form-settings");
  if (!el || !form) return;
  const text = electrsHint(form.electrs_host.value) || serverText || "";
  el.textContent = text;
  el.classList.toggle("hidden", !text);
}

async function loadSettings() {
  const s = await api("/api/settings");
  const form = $("#form-settings");
  form.eth_rpc_url.value = s.eth_rpc_url || "";
  form.electrs_host.value = s.electrs_host || "";
  form.electrs_port.value = s.electrs_port || 50001;
  showElectrsHint(s.electrs_hint);
  if (form.btc_gap_limit) form.btc_gap_limit.value = s.btc_gap_limit || 50;
  if (form.outflow_drop_pct) form.outflow_drop_pct.value = s.outflow_drop_pct ?? 5;
  if (form.outflow_usd) form.outflow_usd.value = s.outflow_usd ?? 500;
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
  $("#btn-refresh").addEventListener("click", () => load(true, { full: true }));
  $("#btn-settings").addEventListener("click", async () => { await loadSettings(); openSettings(true); });
  const electrsHost = document.querySelector("#form-settings [name=electrs_host]");
  if (electrsHost) electrsHost.addEventListener("input", () => showElectrsHint(""));
  $("#btn-close-settings").addEventListener("click", () => openSettings(false));
  $("#settings-backdrop").addEventListener("click", () => openSettings(false));
  $("#form-settings").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const f = formData(ev.target);
    const body = {
      eth_rpc_url: f.eth_rpc_url,
      electrs_host: f.electrs_host,
      electrs_port: Number(f.electrs_port || 50001),
      btc_gap_limit: Number(f.btc_gap_limit || 50),
      outflow_drop_pct: Number(f.outflow_drop_pct || 5),
      outflow_usd: Number(f.outflow_usd || 0),
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
  const secDetails = $("#security-details");
  if (secDetails) {
    secDetails.addEventListener("toggle", () => {
      if (secDetails.open) loadSecurity(false);
    });
  }
  const secRefresh = $("#btn-sec-refresh");
  if (secRefresh) secRefresh.addEventListener("click", (ev) => { ev.preventDefault(); loadSecurity(true); });
  const switchHidden = $("#btn-switch-hidden");
  if (switchHidden) switchHidden.addEventListener("click", () => {
    state.showHiddenSwitches = !state.showHiddenSwitches;
    if (state.data) renderSwitches(state.data);
  });
  document.addEventListener("click", async (ev) => {
    const hide = ev.target.closest("[data-switch-hide]");
    const unhide = ev.target.closest("[data-switch-unhide]");
    const button = hide || unhide;
    if (!button) return;
    try {
      await api(hide ? "/api/switches/hide" : "/api/switches/unhide", {
        method: "POST",
        body: JSON.stringify({
          owner: button.dataset.owner,
          from_position: button.dataset.from,
          to_target: button.dataset.to,
          spread: Number(button.dataset.spread),
        }),
      });
      await load(false);
    } catch (err) { alert(err.message); }
  });
  const secHidden = $("#btn-sec-hidden");
  if (secHidden) secHidden.addEventListener("click", (ev) => {
    ev.preventDefault();
    state.showHiddenApprovals = !state.showHiddenApprovals;
    renderSecurity();
  });
  document.addEventListener("click", async (ev) => {
    const jump = ev.target.closest("[data-goto-security]");
    if (jump) {
      ev.preventDefault();
      const details = $("#security-details");
      if (details) {
        details.open = true;
        details.scrollIntoView({ behavior: "smooth", block: "start" });
        loadSecurity(false);
      }
      return;
    }
    const hide = ev.target.closest("[data-approval-hide]");
    const unhide = ev.target.closest("[data-approval-unhide]");
    const button = hide || unhide;
    if (!button) return;
    try {
      await api(hide ? "/api/security/hide" : "/api/security/unhide", {
        method: "POST",
        body: JSON.stringify({
          wallet: button.dataset.wallet,
          chain: button.dataset.chain,
          token: button.dataset.token,
          spender: button.dataset.spender,
          allowance: button.dataset.allowance,
        }),
      });
      state.security = null;
      await loadSecurity(false);
    } catch (err) { alert(err.message); }
  });
  const nftDetails = $("#nft-details");
  if (nftDetails) {
    nftDetails.addEventListener("toggle", () => {
      if (nftDetails.open) loadNfts(false);
    });
  }
  const nftRefresh = $("#btn-nft-refresh");
  if (nftRefresh) {
    nftRefresh.addEventListener("click", (ev) => {
      ev.preventDefault();
      loadNfts(true);
    });
  }
  $("#btn-nft-hidden").addEventListener("click", () => {
    state.showHiddenNfts = !state.showHiddenNfts;
    renderNfts();
  });
  document.addEventListener("click", async (ev) => {
    const hide = ev.target.closest("[data-nft-hide]");
    const unhide = ev.target.closest("[data-nft-unhide]");
    const button = hide || unhide;
    if (!button) return;
    try {
      await api(hide ? "/api/nfts/hide" : "/api/nfts/unhide", {
        method: "POST",
        body: JSON.stringify({
          chain: button.dataset.chain,
          contract: button.dataset.contract,
          token_id: button.dataset.token,
          kind: button.dataset.kind,
        }),
      });
      state.nfts = null;
      await loadNfts(true);
    } catch (err) { alert(err.message); }
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
      const warning = data.warning || "Il file contiene i descrittori Bitcoin di sola osservazione. Non inviarlo.";
      if (!window.confirm(warning + "\n\nScaricare il file?")) return;
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
  const posDetails = $("#positions-details");
  if (posDetails) {
    posDetails.addEventListener("toggle", () => {
      if (applyingPositions) return;
      api("/api/settings", {
        method: "PUT",
        body: JSON.stringify({ positions_open: posDetails.open }),
      }).catch(() => {
        const existing = (state.data && state.data.errors) || [];
        showBanner(existing.concat(["Non sono riuscito a ricordare se Posizioni è aperta. Gli indirizzi non sono stati toccati."]));
      });
    });
  }
  await load(false);
  load(true);
}

boot();
