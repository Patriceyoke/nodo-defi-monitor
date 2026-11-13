/* Monitor DeFi frontend — original, Italian, read-only. */
const PALETTE = ["#0D9488", "#2563EB", "#F59E0B", "#7C3AED", "#06B6D4", "#E11D48", "#64748B"];
const OWNER_LABEL = { io: "Io", moglie: "Moglie", tutti: "Combinato" };
const state = { owner: "tutti", chartOwner: "tutti", histOwner: "tutti", histDays: 7, currency: "USD", alloc: "protocollo", data: null, fx: null, nfts: null, showHiddenNfts: false, showHiddenApprovals: false, showHiddenSwitches: false, refreshing: false, stale: true, positionsOpenApplied: false };
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
  const approvazioni = approvals === 1 ? "1 approvazione da fare" : `${approvals} approvazioni da fare`;
  const avvisi = other === 1 ? "1 avviso" : `${other} avvisi`;
  if (approvals && other) return `${approvazioni} e ${avvisi} da controllare`;
  if (approvals) return approvazioni;
  return `${avvisi} da controllare`;
}

function liveSecuritySummary(fallback) {
  const data = state.security;
  if (data && data.ready) {
    const approvals = securityRows(data.approvals).filter((row) => !row.hidden && row.risk_class === "rischiosa");
    const outflows = securityRows(data.outflows);
    const incidents = (data.incidents || []).filter((row) => state.owner === "tutti" || (row.owners || []).includes(state.owner));
    return { approvals: approvals.length, incidents: incidents.length, outflows: outflows.length, visible: approvals.length + incidents.length + outflows.length };
  }
  const pack = fallback || {};
  if (pack[state.owner]) return pack[state.owner];
  return pack.view || pack;
}

function dismissedAlerts(data) {
  const raw = data && data.alert_dismissed;
  return Array.isArray(raw) ? raw : [];
}
function currentRiskyKeys(summary) {
  return (summary && summary.risky_keys) || [];
}
function bannerHasNew(summary, data) {
  const current = currentRiskyKeys(summary);
  if (!current.length) return false;
  const known = new Set(dismissedAlerts(data));
  return current.some((key) => !known.has(key));
}
function renderSecuritySummary(summary) {
  const box = $("#security-summary");
  if (!box) return;
  const approvals = summary && Number(summary.approvals || 0);
  if (!approvals || !bannerHasNew(summary, state.data)) {
    box.innerHTML = "";
    return;
  }
  box.innerHTML = `<div class="callout warn alert-banner"><a href="#wallet-security" data-goto-security>${escapeHtml(securityLine(summary))} · Vai a Sicurezza</a><button class="btn ghost" id="btn-dismiss-alert" type="button">Nascondi avviso</button></div>`;
}
async function dismissAlert() {
  const pack = (state.data && state.data.security_summary) || {};
  const tutti = (pack.tutti && pack.tutti.risky_keys) || [];
  const view = currentRiskyKeys(liveSecuritySummary(pack));
  const keys = [...new Set([...tutti, ...view])];
  if (state.data) state.data.alert_dismissed = keys;
  renderSecuritySummary(liveSecuritySummary(pack));
  try {
    await api("/api/settings", { method: "PUT", body: JSON.stringify({ alert_dismissed: keys }) });
  } catch (err) {
    showBanner([err && err.message ? err.message : "Non sono riuscito a nascondere l'avviso."]);
  }
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

function renderSplit(data) {
  const owner = state.chartOwner || "tutti";
  const rows = (data.positions || []).filter((row) => owner === "tutti" || row.owner === owner);
  const grouped = new Map();
  rows.forEach((row) => {
    const value = Number(row.usd) || 0;
    if (value <= 0) return;
    const label = `${row.protocol || "Posizione"} · ${row.asset || ""}`.trim();
    grouped.set(label, (grouped.get(label) || 0) + value);
  });
  const parts = [...grouped.entries()].sort((a, b) => b[1] - a[1]);
  const total = parts.reduce((sum, item) => sum + item[1], 0);
  const legend = $("#chart-legend");
  const canvas = document.getElementById("chart-split");
  if (!parts.length || !canvas) {
    if (charts["chart-split"]) { charts["chart-split"].destroy(); charts["chart-split"] = null; }
    if (legend) legend.innerHTML = `<li class="mini">Nessuna posizione con un valore in questa vista.</li>`;
    return;
  }
  const colors = parts.map((_, index) => PALETTE[index % PALETTE.length]);
  if (legend) {
    legend.innerHTML = parts.map(([label, value], index) => `
      <li><span class="swatch" style="background:${colors[index]}"></span>${escapeHtml(label)} · ${pct((value / total) * 100)}</li>
    `).join("");
  }
  paintChart("chart-split", {
    type: "doughnut",
    data: { labels: parts.map(([label]) => label), datasets: [{ data: parts.map((item) => item[1]), backgroundColor: colors, borderWidth: 0 }] },
    options: {
      plugins: { legend: { display: false }, tooltip: { callbacks: { label: (c) => ` ${c.label}: ${money(c.raw)} · ${pct((c.raw / total) * 100)}` } } },
      cutout: "58%",
    },
  });
}
const ETH_ASSETS = ["wsteth", "weeth", "steth", "weth", "cbeth", "rseth", "ezeth", "reth", "meth", "eth"];
const STABLE_ASSETS = ["crvusd", "pyusd", "usdc", "usdt", "usds", "lusd", "frax", "gho", "dai"];
function assetKind(asset) {
  const text = String(asset || "").toLowerCase().replace(/[^a-z0-9]/g, "");
  if (ETH_ASSETS.some((name) => text === name || text.endsWith(name))) return "eth";
  if (STABLE_ASSETS.some((name) => text === name || text.endsWith(name))) return "stable";
  return "other";
}
function groupIncome(rows) {
  const usd = rows.reduce((sum, row) => sum + (Number(row.usd) || 0), 0);
  const year = rows.reduce((sum, row) => sum + ((Number(row.usd) || 0) * (Number(row.apy) || 0) / 100), 0);
  return { usd, year, month: year / 12, apy: usd ? (year / usd) * 100 : 0 };
}
function splitBlock(label, stats, total) {
  const share = total > 0 ? pct((stats.usd / total) * 100) : "–";
  return `<div class="split-group">
    <h4>${label}</h4>
    <dl>
      <dt>Valore</dt><dd>${money(stats.usd)} · ${share}</dd>
      <dt>APY ponderato</dt><dd>${pct(stats.apy)}</dd>
      <dt>Reddito mensile</dt><dd>${money(stats.month)}</dd>
      <dt>Reddito annuo</dt><dd>${money(stats.year)}</dd>
    </dl>
  </div>`;
}
function renderMetrics(data) {
  const box = $("#owner-cards");
  if (!box) return;
  const totals = data.totals || {};
  const positions = data.positions || [];
  const keys = ["io", "moglie", "tutti"];
  box.innerHTML = keys.map((key) => {
    const row = totals[key] || {};
    const apy = row.wapy == null ? "–" : pct(row.wapy);
    const year = row.year_usd == null ? "–" : money(row.year_usd);
    const month = row.year_usd == null ? "–" : money(Number(row.year_usd) / 12);
    const rows = positions.filter((item) => key === "tutti" || item.owner === key);
    const stable = groupIncome(rows.filter((item) => assetKind(item.asset) === "stable"));
    const eth = groupIncome(rows.filter((item) => assetKind(item.asset) === "eth"));
    const base = Number(row.total_usd) || (stable.usd + eth.usd);
    return `<article class="owner-card${key === "tutti" ? " sum" : ""}">
      <h3>${OWNER_LABEL[key] || key}</h3>
      <div class="figure">${money(row.total_usd || 0)}</div>
      <dl>
        <dt>APY ponderato</dt><dd>${apy}</dd>
        <dt>Reddito mensile</dt><dd>${month}</dd>
        <dt>Reddito annuo</dt><dd>${year}</dd>
      </dl>
      <div class="split-row">
        ${splitBlock("Stablecoin", stable, base)}
        ${splitBlock("ETH", eth, base)}
      </div>
    </article>`;
  }).join("");
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

function formatHistDate(ts, withYear) {
  const date = new Date(Number(ts) * 1000);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleDateString("it-IT", withYear
    ? { day: "numeric", month: "short", year: "numeric" }
    : { day: "numeric", month: "short" });
}
function historyPoints(data) {
  const days = Number(state.histDays) || 7;
  const owner = state.histOwner || "tutti";
  const now = Number(data.ts) || Math.floor(Date.now() / 1000);
  const cutoff = now - days * 86400;
  const byDay = new Map();
  (data.history || [])
    .filter((row) => Number(row.ts) >= cutoff && (row.owner || "tutti") === owner)
    .sort((a, b) => Number(a.ts) - Number(b.ts))
    .forEach((row) => {
      const date = new Date(Number(row.ts) * 1000);
      const key = date.getFullYear() + "-" + date.getMonth() + "-" + date.getDate();
      const value = Number(row.total_usd != null ? row.total_usd : row.defi_usd);
      byDay.set(key, { ts: Number(row.ts), value: Number.isFinite(value) ? value : 0 });
    });
  return { days, owner, cutoff, points: [...byDay.values()] };
}
function renderHist(data) {
  const series = historyPoints(data);
  const label = OWNER_LABEL[series.owner] || "Combinato";
  const caption = $("#hist-caption");
  const empty = $("#hist-empty");
  const box = $("#hist-chart-box");
  const points = series.points;
  if (!points.length) {
    if (charts["chart-hist"]) { charts["chart-hist"].destroy(); charts["chart-hist"] = null; }
    if (caption) caption.textContent = label + " · ultimi " + series.days + " giorni. Il selettore cambia solo questo grafico.";
    if (box) box.classList.add("hidden");
    if (empty) {
      empty.classList.remove("hidden");
      empty.textContent = "Ancora nessuno storico in questo periodo. Resta dopo il primo aggiornamento riuscito.";
    }
    return;
  }
  if (box) box.classList.remove("hidden");
  if (empty) empty.classList.add("hidden");
  const from = points[0].ts > series.cutoff + 86400 ? " · dati dal " + formatHistDate(points[0].ts) : "";
  if (caption) caption.textContent = label + " · ultimi " + series.days + " giorni" + from + ". Il selettore cambia solo questo grafico.";
  paintChart("chart-hist", {
    type: "line",
    data: {
      labels: points.map((point) => formatHistDate(point.ts)),
      datasets: [{
        label: "Valore",
        data: points.map((point) => point.value),
        borderColor: "#0D9488",
        backgroundColor: "rgba(13, 148, 136, 0.12)",
        fill: true,
        tension: 0.25,
        pointRadius: points.length > 24 ? 0 : 3,
        pointHoverRadius: 4,
        borderWidth: 2,
      }],
    },
    options: {
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            title: (items) => formatHistDate(points[items[0].dataIndex].ts, true),
            label: (item) => "Valore: " + money(item.parsed.y),
            afterLabel: (item) => {
              const prev = points[item.dataIndex - 1];
              if (!prev) return "Variazione giornaliera: –";
              const delta = item.parsed.y - prev.value;
              const sign = delta > 0 ? "+" : "";
              return "Variazione giornaliera: " + sign + money(delta);
            },
          },
        },
      },
      scales: {
        x: { grid: { display: false }, ticks: { maxRotation: 0, autoSkip: true, maxTicksLimit: 6 } },
        y: { ticks: { maxTicksLimit: 4, callback: (value) => money(value) }, grace: "10%" },
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

function incomeAmounts(row) {
  if (row.apy == null || row.usd == null || !Number(row.apy)) return null;
  const year = Number(row.usd) * Number(row.apy) / 100;
  return { year, month: year / 12 };
}
function positionTable(title, rows) {
  const totals = groupIncome(rows);
  const body = rows.length ? rows.map((p) => {
    const income = incomeAmounts(p);
    return `<tr>
      <td>${escapeHtml(p.protocol || "")}</td>
      <td>${escapeHtml(p.asset || "")}</td>
      <td class="num">${p.usd == null ? "n/d" : money(p.usd)}</td>
      <td class="num">${p.apy == null ? "–" : pct(p.apy)}</td>
      <td class="num">${p.apy30 == null ? "–" : pct(p.apy30)}</td>
      <td class="num">${income ? money(income.month) : "–"}</td>
      <td class="num">${income ? money(income.year) : "–"}</td>
    </tr>`;
  }).join("") + `<tr class="sum">
      <td colspan="2">Totale</td>
      <td class="num">${money(totals.usd)}</td>
      <td class="num">${pct(totals.apy)}</td>
      <td></td>
      <td class="num">${money(totals.month)}</td>
      <td class="num">${money(totals.year)}</td>
    </tr>` : `<tr><td colspan="7" class="mini">Nessuna posizione.</td></tr>`;
  return `<div class="pos-block">
    <h3>${escapeHtml(title)}</h3>
    <div class="table-scroll">
      <table class="positions">
        <thead>
          <tr>
            <th scope="col">Protocollo</th>
            <th scope="col">Asset</th>
            <th scope="col" class="num">Valore</th>
            <th scope="col" class="num">APY</th>
            <th scope="col" class="num">Media 30 giorni</th>
            <th scope="col" class="num">Reddito mensile</th>
            <th scope="col" class="num">Reddito annuo</th>
          </tr>
        </thead>
        <tbody>${body}</tbody>
      </table>
    </div>
  </div>`;
}
function renderPositions(data) {
  const summary = $("#pos-summary");
  const pos = data.positions || [];
  if (summary) {
    summary.textContent = pos.length
      ? `${pos.length} posizioni nella vista ${OWNER_LABEL[state.owner]}.`
      : "Nessuna posizione in questa vista.";
  }
  const host = $("#pos-groups");
  if (!host) return;
  const groups = state.owner === "tutti" ? ["io", "moglie"] : [state.owner];
  host.innerHTML = groups.map((owner) => {
    const rows = pos.filter((row) => row.owner === owner).slice().sort((a, b) => (Number(b.usd) || 0) - (Number(a.usd) || 0));
    return positionTable(OWNER_LABEL[owner] || owner, rows);
  }).join("");
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
  const all = (data.switches || []).filter((item) => state.owner === "tutti" || item.owner === state.owner);
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

function renderRows(el, rows) {
  if (!el) return;
  if (!rows.length) {
    el.innerHTML = `<p class="mini">Nessun elemento in questa vista.</p>`;
    return;
  }
  el.innerHTML = rows.map((s) => `
    <div class="rowitem">
      <div><div class="title">${escapeHtml(s.title || s.protocol)}</div><div class="detail">${escapeHtml(s.detail || "")}</div></div>
      <span class="pill ${toneClass(s.tone)}">${escapeHtml(s.label || "")}</span>
    </div>`).join("");
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
  const allApprovals = securityRows(data.approvals).slice().sort((a, b) => (a.risk_class === "rischiosa" ? 0 : 1) - (b.risk_class === "rischiosa" ? 0 : 1));
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
      <h3>${escapeHtml(row.symbol || "Token")} · ${escapeHtml(row.chain || "")} <span class="pill ${row.risk_class === "rischiosa" ? "bad" : "ok"}">${row.risk_class === "rischiosa" ? "Rischiosa" : "Normale"}</span></h3>
      <p class="mini">${escapeHtml(row.label || "")} · ${OWNER_LABEL[row.owner] || ""} · ${escapeHtml(row.risk_reason || flagText(row.flags) || "nessun segnale")}</p>
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
  data.errors = data.errors || [];
  data.history = data.history || [];
  data.security = data.security || [];
  try {
    if (data.currency === "USD" || data.currency === "THB") state.currency = data.currency;
    state.fx = data.fx_thb || null;
    document.querySelectorAll('[data-key="currency"] button').forEach((button) => {
      button.classList.toggle("on", button.dataset.value === state.currency);
    });
    $("#empty-state").classList.toggle("hidden", !!(data.totals.tutti.total_usd || data.positions.length || data.wallet_count));
    renderMetrics(data);
    renderSplit(data);
    renderPositions(data);
    renderHist(data);
    renderSwitches(data);
    const ensRows = (data.ens_alerts || []).map((item) => ({
      title: (item.name || "ENS") + (OWNER_LABEL[item.owner] ? " · " + OWNER_LABEL[item.owner] : ""),
      detail: item.text || "",
      tone: item.level === "danger" ? "danger" : "warning",
      label: item.level === "danger" ? "scaduto" : "in scadenza",
    }));
    renderRows($("#security"), ensRows.concat(data.security));
    const banners = [...data.errors];
    if ((data.wallet_count || 0) > 0 && !data.positions.length && !state.refreshing) {
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
  const path = force
    ? "/api/refresh?owner=tutti" + (full ? "&full=1" : "")
    : "/api/portfolio?owner=tutti";
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
    if (data.fx_thb) state.fx = data.fx_thb;
    render();
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

function openSettings(open) {
  $("#settings").classList.toggle("hidden", !open);
  $("#settings-backdrop").classList.toggle("hidden", !open);
}

async function loadSettings() {
  const s = await api("/api/settings");
  const form = $("#form-settings");
  form.eth_rpc_url.value = s.eth_rpc_url || "";
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
  $("#token-line").textContent = "Token MCP (mascherato): " + (s.mcp_token_masked || "—") + ". Lo vedi per intero solo quando lo generi di nuovo.";
  if (s.mcp_token) {
    $("#token-line").textContent = "Nuovo token MCP (copialo ora): " + s.mcp_token;
  }
  const wallets = await api("/api/wallets");
  $("#evm-list").innerHTML = (wallets.evm || []).map((w) =>
    `<p class="mini">${escapeHtml(w.label)} · ${OWNER_LABEL[w.owner]} · ${escapeHtml(w.address)} <button class="btn" data-del-evm="${w.id}" type="button">Rimuovi</button></p>`
  ).join("") || `<p class="mini">Nessun indirizzo Ethereum.</p>`;
}

function formData(form) {
  return Object.fromEntries(new FormData(form).entries());
}

async function boot() {
  commonChart();
  bindSegmented();
  $("#btn-refresh").addEventListener("click", () => load(true, { full: true }));
  document.addEventListener("click", (ev) => {
    const dismiss = ev.target.closest("#btn-dismiss-alert");
    if (dismiss) { ev.preventDefault(); dismissAlert(); }
    const chartBtn = ev.target.closest("#chart-owner button");
    if (chartBtn) {
      state.chartOwner = chartBtn.dataset.value;
      chartBtn.parentElement.querySelectorAll("button").forEach((button) => button.classList.toggle("on", button === chartBtn));
      if (state.data) renderSplit(state.data);
      return;
    }
    const histOwner = ev.target.closest("#hist-owner button");
    if (histOwner) {
      state.histOwner = histOwner.dataset.value;
      histOwner.parentElement.querySelectorAll("button").forEach((button) => button.classList.toggle("on", button === histOwner));
      if (state.data) renderHist(state.data);
      return;
    }
    const histRange = ev.target.closest("#hist-range button");
    if (!histRange) return;
    state.histDays = Number(histRange.dataset.value) || 7;
    histRange.parentElement.querySelectorAll("button").forEach((button) => button.classList.toggle("on", button === histRange));
    if (state.data) renderHist(state.data);
  });
  $("#btn-settings").addEventListener("click", async () => { await loadSettings(); openSettings(true); });
  $("#btn-close-settings").addEventListener("click", () => openSettings(false));
  $("#settings-backdrop").addEventListener("click", () => openSettings(false));
  $("#form-settings").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const f = formData(ev.target);
    const body = {
      eth_rpc_url: f.eth_rpc_url,
      outflow_drop_pct: Number(f.outflow_drop_pct || 5),
      outflow_usd: Number(f.outflow_usd || 0),
      gas_estimate_usd: f.gas_estimate_usd === "" ? 0 : Number(f.gas_estimate_usd),
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
  function addressImportMessage(result) {
    const added = Number(result.added || 0);
    const skipped = Number(result.skipped || 0);
    const invalid = Number(result.invalid || 0);
    const parts = [added === 1 ? "Aggiunto 1 indirizzo." : `Aggiunti ${added} indirizzi.`];
    if (skipped === 1) parts.push("1 già presente, ignorato.");
    else if (skipped) parts.push(`${skipped} già presenti, ignorati.`);
    if (invalid === 1) parts.push("1 non valido.");
    else if (invalid) parts.push(`${invalid} non validi.`);
    const extra = (result.errors || []).filter(Boolean).join(" ");
    return parts.join(" ") + (extra ? " " + extra : "");
  }
  async function importAddresses(body) {
    const result = await api("/api/addresses/import", { method: "POST", body: JSON.stringify(body) });
    const message = addressImportMessage(result);
    const line = $("#import-addresses-result");
    if (line) line.textContent = message;
    alert(message);
    await loadSettings();
    if (Number(result.added || 0) > 0) await load(true);
  }
  $("#btn-export-addresses").addEventListener("click", async () => {
    try {
      const data = await api("/api/addresses/export");
      const file = {
        kind: "nodo-defi-monitor-addresses",
        version: 1,
        addresses: (data.addresses || []).map((row) => ({
          address: row.address,
          owner: row.owner,
          label: row.label,
        })),
      };
      const blob = new Blob([JSON.stringify(file, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "nodo-defi-monitor-indirizzi.json";
      a.click();
      URL.revokeObjectURL(url);
    } catch (err) { alert(err.message); }
  });
  $("#import-addresses-file").addEventListener("change", async (ev) => {
    const file = ev.target.files && ev.target.files[0];
    ev.target.value = "";
    if (!file) return;
    try {
      const text = await file.text();
      let body;
      try {
        const parsed = JSON.parse(text);
        if (Array.isArray(parsed)) body = { addresses: parsed };
        else if (parsed && typeof parsed === "object") body = parsed;
        else body = { text };
      } catch (_) {
        body = { text };
      }
      await importAddresses(body);
    } catch (err) { alert(err.message); }
  });
  $("#btn-import-addresses").addEventListener("click", async () => {
    const text = ($("#import-addresses-text") && $("#import-addresses-text").value || "").trim();
    if (!text) {
      alert("Incolla almeno una riga: indirizzo, io oppure moglie, etichetta facoltativa.");
      return;
    }
    try {
      await importAddresses({ text });
      $("#import-addresses-text").value = "";
    } catch (err) { alert(err.message); }
  });
  $("#btn-export").addEventListener("click", async () => {
    try {
      const data = await api("/api/backup");
      const warning = data.warning || "Il file contiene gli indirizzi Ethereum pubblici. Non inviarlo se non ti serve.";
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
      alert(`Importati ${result.evm_added} indirizzi. I portafogli già presenti non sono stati cancellati.`);
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
  document.addEventListener("click", async (ev) => {
    const evm = ev.target.closest("[data-del-evm]");
    if (!evm) return;
    try {
      await api("/api/wallets/evm/" + evm.dataset.delEvm, { method: "DELETE" });
      await loadSettings();
      await load(true);
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
