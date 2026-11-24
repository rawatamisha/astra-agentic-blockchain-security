"use strict";

/* ---------- small helpers ---------- */
const $ = (sel, root = document) => root.querySelector(sel);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const short = (id, n = 6) => (id && id.length > 2 * n ? `${id.slice(0, n)}…${id.slice(-4)}` : id);
const fx = (v, d = 3) => (v === null || v === undefined ? "–" : Number(v).toFixed(d));
const pct = (v, d = 0) => (v === null || v === undefined ? "–" : `${(v * 100).toFixed(d)}%`);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

async function api(path, body) {
  const res = await fetch(path, body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).detail || msg; } catch (_) { /* keep status text */ }
    throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
  }
  return res.json();
}

function toast(text) {
  const t = $("#toast");
  t.textContent = text;
  t.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => t.classList.remove("show"), 2600);
}

/* ---------- static content ---------- */
const SCEN = {
  normal: { title: "Honest node", text: "Ordinary payments, four peers in four groups", kind: "honest", label: "Honest" },
  sybil: { title: "Sybil attacker", text: "Three sibling identities funded at the same instant", kind: "attack", label: "Attack" },
  eclipse: { title: "Eclipse attacker", text: "All four proposed peers come from one group", kind: "attack", label: "Attack" },
  wallet: { title: "Abnormal wallet", text: "28 identical 500-unit transfers, one tick apart", kind: "attack", label: "Attack" },
  conflict: { title: "Finney / double spend", text: "Nonce 0 seen going to two different recipients", kind: "attack", label: "Attack" },
  sparse: { title: "Brand-new identity", text: "No history at all — nothing to inspect", kind: "gap", label: "No evidence" },
};
const ICON = {
  sybil: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="9" cy="8" r="3"/><circle cx="17" cy="9" r="2.4"/><path d="M3 20c0-3.3 2.7-6 6-6s6 2.7 6 6M15 14.5c3 0 6 1.8 6 5"/></svg>',
  wallet: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="6" width="18" height="13" rx="2"/><path d="M3 10h18M16 14h2"/></svg>',
  eclipse: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/><circle cx="12" cy="12" r="8" stroke-dasharray="3 3"/></svg>',
  finney: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 7h11l-3-3M20 17H9l3 3"/></svg>',
};
const AGENT = {
  sybil: { t: "Sybil specialist", q: "Is this one of many fake identities run by one operator?" },
  wallet: { t: "Wallet specialist", q: "Does the wallet behave like ordinary users? (Isolation Forest)" },
  eclipse: { t: "Eclipse specialist", q: "Could its peers cut honest nodes off from the network?" },
  finney: { t: "Finney specialist", q: "Has it tried to spend the same coins twice?" },
};
const CATEGORY = (r) => r === null || r === undefined ? ["No score", "neutral"]
  : r >= 0.75 ? ["Strong indicator", "bad"] : r >= 0.5 ? ["Moderate", "warn"]
  : r >= 0.25 ? ["Weak", "warn"] : ["No indicator", "good"];
const BEHAVE = [
  { mode: "ordinary", t: "Honest transactions", s: "normal payments, fair block share" },
  { mode: "concentration", t: "51% majority", s: "produces 3 of every 5 blocks", atk: true },
  { mode: "conflicts", t: "Double spend", s: "re-spends a nonce to another wallet", atk: true },
  { mode: "release", t: "Selfish mining", s: "hides blocks, releases them in bursts", atk: true },
  { mode: "pool_gap", t: "Block withholding", s: "drops the blocks it owes its pool", atk: true },
];
const CHECK = {
  majority: { t: "51% / majority", rule: "indicator if share > 50% of ≥ 20 blocks" },
  double_spend: { t: "Double spend", rule: "indicator on any conflicting spend" },
  selfish_mining: { t: "Selfish mining", rule: "indicator if ≥ 2 samples show tips + bursts" },
  withholding: { t: "Block withholding", rule: "indicator if yield < 20% of expected" },
};

/* ---------- state ---------- */
const S = {
  state: null, stage: "network", selected: "normal", current: null, cand: null,
  member: null, mode: "ordinary", auto: false, timer: null, busy: false,
  slots: [], txs: [], mon: null, count: 0, flagged: false, leaving: null,
  log: new Map(), lastHeight: null, trigger: null,
};

/* ---------- stepper ---------- */
const ORDER = ["network", "request", "layer1", "decision", "layer2", "outcome"];
function setStep(active, alert = false) {
  const idx = ORDER.indexOf(active);
  document.querySelectorAll(".step").forEach((el) => {
    const i = ORDER.indexOf(el.dataset.step);
    el.classList.toggle("active", i === idx);
    el.classList.toggle("done", i < idx);
    el.classList.toggle("alert", alert && i === idx);
  });
}

/* ---------- network drawing ---------- */
const NS = "http://www.w3.org/2000/svg";
const CX = 300, CY = 270, R = 140, ORBIT = R + 95;
let POS = {};

function layout(nodes, edges) {
  const pos = {};
  const relays = nodes.filter((n) => n.kind === "relay");
  relays.forEach((n) => {
    const i = Number(n.id.split("-")[1]);
    const a = (-90 + i * 60) * Math.PI / 180;
    pos[n.id] = { x: CX + R * Math.cos(a), y: CY + R * Math.sin(a), a };
  });
  const members = nodes.filter((n) => n.kind === "member");
  members.forEach((n, k) => {
    const peers = edges.filter((e) => !e.proposed && (e.source === n.id || e.target === n.id))
      .map((e) => (e.source === n.id ? e.target : e.source)).filter((p) => pos[p] && pos[p].a !== undefined);
    let a = peers.length ? Math.atan2(
      peers.reduce((s, p) => s + Math.sin(pos[p].a), 0), peers.reduce((s, p) => s + Math.cos(pos[p].a), 0))
      : -Math.PI / 2;
    a += (k % 2 ? 1 : -1) * Math.ceil(k / 2) * 0.42;
    // keep clear of the "block producer" label above relay-0 and of the candidate slot on the right
    const deg = ((a * 180 / Math.PI) + 360) % 360;
    if (Math.abs(deg - 270) < 28) a = (270 + (deg >= 270 ? 30 : -30)) * Math.PI / 180;
    pos[n.id] = { x: CX + ORBIT * Math.cos(a), y: CY + ORBIT * Math.sin(a) };
  });
  nodes.filter((n) => n.kind === "candidate").forEach((n, k) => {
    pos[n.id] = { x: 668, y: 170 + k * 120 };
  });
  return pos;
}

function el(tag, attrs = {}, parent) {
  const e = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  if (parent) parent.appendChild(e);
  return e;
}

function drawNetwork() {
  const svg = $("#net");
  let graph = $("#graph", svg), pk = $("#pk", svg);
  if (!graph) {
    graph = el("g", { id: "graph" }, svg);
    pk = el("g", { id: "pk" }, svg);
  }
  graph.innerHTML = "";
  const net = S.state.network;
  // show only the candidate being presented
  const nodes = net.nodes.filter((n) => n.kind !== "candidate" || n.id === S.current);
  if (S.leaving && !nodes.find((n) => n.id === S.leaving.id)) nodes.push(S.leaving);
  const edges = net.edges.filter((e) => !e.proposed || e.source === S.current);
  if (S.leaving) S.leaving.edges.forEach((e) => edges.push(e));
  POS = layout(nodes, edges);

  el("text", { x: CX, y: CY - 6, class: "center-big" }, graph).textContent = S.state.ledger.height;
  el("text", { x: CX, y: CY + 16, class: "center-label" }, graph).textContent = "blocks on every replica";

  const memberEdge = (e) => !e.source.startsWith("relay") || !e.target.startsWith("relay");
  const groups = new Set(edges.filter((e) => e.proposed).map((e) => e.group));
  for (const e of edges) {
    const a = POS[e.source], b = POS[e.target];
    if (!a || !b) continue;
    let cls = "edge";
    if (e.proposed) cls += " proposed" + (groups.size === 1 && edges.filter((x) => x.proposed).length > 1 ? " concentrated" : "");
    else if (memberEdge(e)) cls += " member";
    const ln = el("line", { x1: a.x, y1: a.y, x2: b.x, y2: b.y, class: cls }, graph);
    if (S.leaving && S.leaving.edges.includes(e)) ln.setAttribute("class", cls + " leaving");
  }

  let m = 0;
  for (const n of nodes) {
    const p = POS[n.id];
    let kind = n.kind;
    if (kind === "member" && S.flagged && n.id === S.member) kind = "flagged";
    if (S.leaving && n.id === S.leaving.id) kind = "flagged leaving";
    const g = el("g", { class: `node ${kind}`, transform: `translate(${p.x},${p.y})` }, graph);
    const r = n.kind === "relay" ? 24 : 21;
    if (n.kind === "candidate" || kind.startsWith("flagged")) el("circle", { r, class: "halo" }, g);
    el("circle", { r, class: "body" }, g);
    const inner = el("text", { class: "inner", "text-anchor": "middle", dy: 4 }, g);
    const label = el("text", { "text-anchor": "middle", dy: r + 17 }, g);
    if (n.kind === "relay") {
      inner.textContent = `R${n.id.split("-")[1]}`;
      label.textContent = n.id;
      if (n.id === "relay-0") el("text", { class: "producer", "text-anchor": "middle", dy: -r - 9 }, g).textContent = "BLOCK PRODUCER";
    } else if (n.kind === "member") {
      m += 1;
      inner.textContent = `M${m}`;
      label.textContent = `node ${n.id.slice(0, 6)}`;
    } else {
      inner.textContent = "?";
      label.textContent = `candidate ${n.id.slice(0, 6)}`;
    }
  }
}

function animatePacket(from, to, cls = "", ms = 420) {
  return new Promise((resolve) => {
    const a = POS[from], b = POS[to];
    if (!a || !b) return resolve();
    const c = el("circle", { r: 5.5, class: `packet ${cls}`, cx: a.x, cy: a.y }, $("#pk"));
    const t0 = performance.now();
    const step = (now) => {
      const k = Math.min(1, (now - t0) / ms);
      const e = k < 0.5 ? 2 * k * k : 1 - Math.pow(-2 * k + 2, 2) / 2;
      c.setAttribute("cx", a.x + (b.x - a.x) * e);
      c.setAttribute("cy", a.y + (b.y - a.y) * e);
      if (k < 1) requestAnimationFrame(step); else { c.remove(); resolve(); }
    };
    requestAnimationFrame(step);
  });
}

async function propagateBlock(cls = "") {
  // breadth-first from the block producer, level by level, like Network.publish
  const edges = S.state.network.edges.filter((e) => !e.proposed);
  const adj = {};
  for (const e of edges) {
    (adj[e.source] ||= []).push(e.target);
    (adj[e.target] ||= []).push(e.source);
  }
  let frontier = ["relay-0"];
  const seen = new Set(frontier);
  while (frontier.length) {
    const next = [], hops = [];
    for (const u of frontier) for (const v of adj[u] || []) {
      if (!seen.has(v)) { seen.add(v); next.push(v); hops.push(animatePacket(u, v, cls, 360)); }
    }
    await Promise.all(hops);
    frontier = next;
  }
}

/* ---------- ledger + stats ---------- */
function drawLedger() {
  const L = S.state.ledger;
  $("#s-replicas").textContent = L.replicas;
  $("#s-height").textContent = L.height;
  $("#s-agree").innerHTML = L.replicas_agree ? '<span style="color:var(--green)">All agree</span>' : '<span style="color:var(--red)">Mismatch</span>';
  $("#s-members").textContent = S.state.network.nodes.filter((n) => n.kind === "member").length;
  const fresh = S.lastHeight !== null && L.height > S.lastHeight;
  $("#chain").innerHTML = L.blocks.map((b, i) => `${i ? '<div class="link"></div>' : ""}
    <div class="block ${fresh && i === L.blocks.length - 1 ? "new" : ""}">
      <div class="h">#${b.height} <span style="color:var(--ink-3);font-weight:400">· ${b.transactions} tx</span></div>
      <div class="hash" title="${b.hash}">${b.hash.slice(0, 14)}…</div>
      <div class="prev" title="${b.previous}">prev ${b.previous.slice(0, 10)}…</div>
    </div>`).join("");
  S.lastHeight = L.height;
  const members = S.state.network.nodes.filter((n) => n.kind === "member").length;
  $("#net-sub").textContent = `Six relay validators in a ring${members ? ` + ${members} admitted member${members > 1 ? "s" : ""}` : ""} · every replica re-validates every block`;
}

async function refresh() {
  S.state = await api("/api/state");
  const a = S.state.analyst, pill = $("#analyst");
  pill.className = `pill ${a.kind === "llm" ? "violet" : "warn"}`;
  pill.textContent = a.kind === "llm" ? `Analyst · ${a.label}` : "Analyst · rule-based stand-in";
  pill.title = a.detail;
  drawNetwork();
  drawLedger();
}

/* ---------- audit log ---------- */
const STATUS_TEXT = {
  PENDING_INVESTIGATION: "Join request registered",
  UNDER_INVESTIGATION: "Layer 1 investigation started",
  AWAITING_ADMIN_REVIEW: "Report ready for the administrator",
  APPROVED: "Administrator approved — node admitted",
  REJECTED: "Administrator rejected the node",
  MORE_EVIDENCE_REQUIRED: "Administrator asked for more evidence",
  REVOKED: "Membership revoked",
};
function addEvents(events, id) {
  for (const ev of events || []) S.log.set(`${id}:${ev.sequence}`, { ...ev, id });
  drawLog();
}
function addLocal(text, cls = "") {
  S.log.set(`local:${Date.now()}:${Math.random()}`, { time: new Date().toISOString(), local: text, cls });
  drawLog();
}
function drawLog() {
  const items = [...S.log.values()].sort((a, b) => (a.time < b.time ? 1 : -1)).slice(0, 12);
  $("#log").innerHTML = items.length ? items.map((e) => {
    const t = new Date(e.time).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    if (e.local) return `<li><time>${t}</time><div style="${e.cls === "bad" ? "color:var(--red)" : ""}">${esc(e.local)}</div></li>`;
    const note = e.detail && e.detail.note ? ` — “${esc(e.detail.note)}”` : "";
    return `<li><time>${t}</time><div>${STATUS_TEXT[e.status] || e.status}${note}<div class="who">node ${short(e.id)}</div></div></li>`;
  }).join("") : '<li class="empty">Nothing yet. Send a join request to begin.</li>';
}

/* ---------- panel: choose a candidate ---------- */
function panelNetwork() {
  setStep("network");
  const P = $("#panel");
  P.innerHTML = `
    <h2>A node wants to join</h2>
    <p class="lead">This is a <b>permissioned</b> blockchain: nobody joins without the administrator's approval.
      ASTRA gives the administrator evidence before admission, and keeps watching after it.</p>
    <ul class="facts">
      <li><span class="n">1</span>Six relays hold their own copy of the chain; relay-0 produces blocks.</li>
      <li><span class="n">2</span>Every transfer is Ed25519-signed with a nonce, so tampering and replays are refused.</li>
      <li><span class="n">3</span>A new node registers a signed identity: node ID = SHA-256 of its public key.</li>
    </ul>
    <div class="sec-title">Pick the node that asks to join</div>
    <div class="scenarios">${Object.entries(SCEN).map(([k, v]) => `
      <button class="scenario ${S.selected === k ? "sel" : ""}" data-k="${k}">
        <span class="kind ${v.kind}">${v.label}</span><b>${v.title}</b><span>${v.text}</span>
      </button>`).join("")}
    </div>
    <div class="row between">
      <span class="sub">The audience doesn't need to know which one you picked — ASTRA has to find out.</span>
      <button class="primary" id="send">Send join request →</button>
    </div>`;
  P.querySelectorAll(".scenario").forEach((b) => b.onclick = () => { S.selected = b.dataset.k; panelNetwork(); });
  $("#send").onclick = sendRequest;
}

/* ---------- Layer 1 ---------- */
async function sendRequest() {
  const btn = $("#send");
  btn.disabled = true;
  setStep("request");
  try {
    const c = await api("/api/candidates", { scenario: S.selected, seed: 41 });
    S.current = c.id;
    S.cand = null;
    await refresh();
    panelLayer1Skeleton();
    // the signed join request travels to the block producer
    await animatePacket(c.id, "relay-0", "", 700);
    await waitForReport();
  } catch (err) {
    toast(err.message);
    panelNetwork();
  }
}

async function waitForReport() {
  let shown = 0;
  for (;;) {
    const c = await api(`/api/candidates/${S.current}`);
    const msgs = c.progress.filter((m) => !m.startsWith("done") && !m.startsWith("error"));
    if (msgs.length > shown) { $("#progress").textContent = msgs[msgs.length - 1]; shown = msgs.length; }
    const err = c.progress.find((m) => m.startsWith("error"));
    if (err) throw new Error(err.slice(6));
    if (c.status === "AWAITING_ADMIN_REVIEW" && c.report) { S.cand = c; break; }
    await sleep(350);
  }
  addEvents(S.cand.events, S.current);
  await refresh();
  await revealLayer1();
}

function panelLayer1Skeleton() {
  setStep("layer1");
  const order = ["wallet", "sybil", "eclipse", "finney"];
  $("#panel").innerHTML = `
    <div class="row between"><h2>Layer 1 · pre-admission investigation</h2><span class="pill info">node ${short(S.current)}</span></div>
    <p class="lead">A LangGraph supervisor sends four specialist agents. Each may only <b>read</b> evidence through tools,
      and every finding must cite a real tool call. Python — not the model — computes the score.</p>
    <div class="sec-title">Step 1 · identity proof</div>
    <div class="idcheck">
      <div class="check" id="id1"><span class="spin"></span>Node ID equals SHA-256 of the node's public key<span class="mono"></span></div>
      <div class="check" id="id2"><span class="spin"></span>Ed25519 signature over (node key, wallet) verifies<span class="mono"></span></div>
      <div class="check" id="id3"><span class="spin"></span>Ledger rebuilt and checked on every replica<span class="mono"></span></div>
    </div>
    <div class="sec-title">Step 2 · specialist agents <span class="sub" id="progress" style="text-transform:none;letter-spacing:0;font-weight:400">Waiting for the investigation…</span></div>
    <div class="agents">${order.map((k) => `
      <div class="agent" id="ag-${k}">
        <div class="top-line"><div class="ic">${ICON[k]}</div>
          <div><div class="t">${AGENT[k].t}</div><div class="q">${AGENT[k].q}</div></div>
          <div class="right"><span class="tick wait">·</span></div></div>
        <div class="body"></div>
      </div>`).join("")}
    </div>
    <div id="tfbox"></div>`;
}

function evidenceFact(k, ev) {
  const e = ev[k];
  if (!e || !e.available) return "No evidence available for this check.";
  if (k === "wallet") {
    const f = e.model.features;
    return `${f.count} outgoing transfers · mean ${Math.round(f.mean_amount)} units · ${f.counterparties} counterparties · Isolation Forest percentile <b>${fx(e.model.lower_tail_percentile, 2)}</b> (anomalous below 0.05)`;
  }
  if (k === "sybil") {
    const n = e.same_timestamp_recipients.length;
    return n ? `<b>${n} other identities</b> were funded by the same treasury at exactly the candidate's funding time.`
      : "No other identity was funded at the candidate's funding time.";
  }
  if (k === "eclipse") {
    return `${e.proposed_degree} proposed peers spread over <b>${e.distinct_groups} group${e.distinct_groups > 1 ? "s" : ""}</b>${e.distinct_groups === 1 ? " — all its links lead into one operator's group" : ""}.`;
  }
  if (k === "finney") {
    return e.conflict ? `<b>Nonce 0 was seen going to ${e.observations.length} different recipients</b> — a conflicting spend.`
      : `${e.observations.length} spend sighting(s), no conflicting spend.`;
  }
  return "";
}

async function tickCheck(id, ok, mono) {
  const row = $(id);
  row.querySelector(".spin, .tick").outerHTML = `<span class="tick ${ok ? "ok" : "no"}">${ok ? "✓" : "✕"}</span>`;
  row.querySelector(".mono").textContent = mono || "";
}

async function revealLayer1() {
  const c = S.cand, rep = c.report;
  if (rep.error) {
    $("#progress").textContent = `Investigation error: ${rep.error}`;
  }
  const ev = rep.evidence || {};
  await sleep(350); await tickCheck("#id1", true, short(c.id, 8));
  await sleep(350); await tickCheck("#id2", true, `${c.signature.slice(0, 12)}…`);
  await sleep(350);
  await tickCheck("#id3", ev.network ? ev.network.replicas_agree : false,
    ev.network ? `${ev.network.replica_count} replicas · ${rep.ledger_blocks} blocks` : "");
  const plan = rep.planning || {};
  $("#progress").textContent = plan.mode === "model" ? `Planner order: ${(rep.order || []).join(" → ")}` : "Planner unavailable — fixed order";

  for (const k of rep.order || []) {
    const box = $(`#ag-${k}`), r = rep.results[k], as = r.assessment;
    box.classList.add("running");
    box.querySelector(".right").innerHTML = '<span class="spin"></span>';
    box.querySelector(".body").innerHTML = `<span class="tool">→ get_${k}_evidence()</span> <span class="sub">reading evidence…</span>`;
    await sleep(850);
    const [cat, tone] = CATEGORY(as.risk);
    const color = tone === "bad" ? "var(--red)" : tone === "warn" ? "var(--amber)" : "var(--green)";
    box.classList.remove("running");
    box.classList.add("done");
    if (as.risk !== null && as.risk >= 0.75) box.classList.add("high");
    box.querySelector(".right").innerHTML = `<span class="pill ${tone}">${cat}</span><b style="font-variant-numeric:tabular-nums">${as.risk === null ? "–" : fx(as.risk, 2)}</b>`;
    const calls = r.calls.map((x) => `${x.tool}#${x.id.slice(-4)}`).join(", ");
    box.querySelector(".body").innerHTML = `${evidenceFact(k, ev)}
      <div class="riskbar"><i style="background:${color}"></i></div>
      <div class="note"><span class="tool">cites ${esc(calls || "no tool call")}</span> · ${esc(as.explanation)}</div>`;
    requestAnimationFrame(() => { box.querySelector(".riskbar i").style.width = `${(as.risk ?? 0) * 100}%`; });
    await sleep(350);
  }
  const review = rep.supervisor_review || {};
  $("#progress").textContent = `Supervisor review: ${review.summary || "–"}`;
  await sleep(300);
  revealTrust();
}

function revealTrust() {
  setStep("decision");
  const t = S.cand.report.trust;
  const box = $("#tfbox");
  const factor = t.factor;
  const rec = t.recommendation;
  const tone = rec === "ACCEPT" ? "good" : rec === "REJECT" ? "bad" : "warn";
  const strong = (t.escalations || []).map((e) => e.dimension);
  const recText = {
    ACCEPT: ["Recommend: admit", "Trust Factor clears 0.70 and no specialist raised a strong indicator."],
    REJECT: ["Recommend: reject", strong.length ? `Strong ${strong.join(", ")} indicator — the policy caps the score below 0.70.` : "Trust Factor is below 0.70."],
    REVIEW_REQUIRED: ["Needs a closer look", "Score clears 0.70 but a specialist escalated."],
    MORE_EVIDENCE: ["More evidence needed", "ASTRA abstains: missing evidence is never treated as low risk."],
  }[rec] || [rec, ""];
  const color = tone === "good" ? "var(--green)" : tone === "bad" ? "var(--red)" : "var(--amber)";
  box.innerHTML = `
    <div class="sec-title">Step 3 · Trust Factor (deterministic Python)</div>
    <div class="tf">
      <div class="row between">
        <div><div class="sub">Trust Factor</div><div class="big" id="tfnum" style="color:${color}">${factor === null ? "–" : "0.000"}</div></div>
        <div style="text-align:right"><div class="sub">Original formula alone</div><div style="font-size:20px;font-weight:600;color:var(--ink-2)">${fx(t.original_factor)}</div>
          <div class="note" style="margin:0">${t.original_factor !== null && t.original_factor >= 0.7 && rec === "REJECT" ? "would have admitted this node" : "&nbsp;"}</div></div>
      </div>
      <div class="gauge"><div class="fill" id="tffill" style="background:${color}"></div>
        ${t.original_factor !== null && t.original_factor !== undefined ? `<div class="ghost" title="original formula" style="left:calc(${t.original_factor * 100}% - 1px)"></div>` : ""}
        <div class="mark" style="left:70%"><span>threshold 0.70</span></div></div>
      <div class="formula">
        <div><span>Positive trust PT</span><b>${fx(t.positive_trust)}</b></div>
        <div><span>Weighted risk AR</span><b>${fx(t.attack_risk)}</b></div>
        <div><span>Peak-adjusted AR</span><b>${fx(t.adjusted_attack_risk)}</b></div>
        <div><span>TF = .6·PT + .4·(1−AR)</span><b>${fx(factor)}</b></div>
      </div>
      <div class="verdict ${tone}"><b>${recText[0]}</b><span>${recText[1]}</span></div>
    </div>
    <div class="sec-title">Step 4 · the administrator decides</div>
    <div class="decide">
      <input type="text" id="note" value="${esc(rec === "ACCEPT" ? `TF ${fx(factor, 2)} above 0.70; all four specialists clear.` : rec === "REJECT" ? `TF ${fx(factor, 2)} below 0.70; strong ${strong.join(", ") || "risk"} indicator.` : "Insufficient evidence to decide.")}">
      <div class="row end">
        <span class="sub" style="margin-right:auto">ASTRA only recommends. A human makes the call.</span>
        <button class="${rec === "ACCEPT" ? "ghost" : "bad"}" id="reject">Reject</button>
        <button class="${rec === "ACCEPT" ? "good" : "ghost"}" id="approve">Approve and admit</button>
      </div>
    </div>`;
  if (factor !== null) {
    const t0 = performance.now();
    const step = (now) => {
      const k = Math.min(1, (now - t0) / 1200), e = 1 - Math.pow(1 - k, 3);
      $("#tfnum").textContent = (factor * e).toFixed(3);
      if (k < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
    requestAnimationFrame(() => { $("#tffill").style.width = `${factor * 100}%`; });
  }
  $("#approve").onclick = () => decide("approve");
  $("#reject").onclick = () => decide("reject");
  box.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

async function decide(action) {
  const note = $("#note").value.trim();
  if (!note) { toast("Write a short reason first"); return; }
  try {
    await api(`/api/candidates/${S.current}/decision`, { action, note });
    const c = await api(`/api/candidates/${S.current}`);
    addEvents(c.events, S.current);
    if (action === "approve") {
      S.member = S.current;
      S.current = null;
      await refresh();
      await propagateBlock("tx"); // treasury funding block for the new member
      startLayer2();
    } else {
      const rejected = S.current;
      S.current = null;
      await refresh();
      panelOutcome("rejected", rejected);
    }
  } catch (err) { toast(err.message); }
}

/* ---------- Layer 2 ---------- */
function startLayer2() {
  S.slots = []; S.txs = []; S.mon = null; S.count = 0; S.flagged = false; S.mode = "ordinary"; S.trigger = null;
  panelLayer2();
  setAuto(true);
}

function setAuto(on) {
  S.auto = on;
  clearInterval(S.timer);
  if (on) S.timer = setInterval(tick, 1000);
  const b = $("#play");
  if (b) b.textContent = on ? "Pause" : "Resume";
}

function panelLayer2() {
  setStep("layer2", S.flagged);
  const P = $("#panel");
  P.innerHTML = `
    <div class="row between"><h2>Layer 2 · continuous monitoring</h2><span class="pill good">member ${short(S.member)}</span></div>
    <p class="lead">Admission is not the end. A <i>sleeper</i> can behave until it is inside. Every transaction the member
      makes produces an observation; ASTRA re-checks the <b>last 10</b> after each one.</p>
    <div class="sec-title">Member behaviour <span style="margin-left:auto"></span></div>
    <div class="behave">${BEHAVE.map((b) => `<button data-mode="${b.mode}" class="${b.atk ? "atk" : ""} ${S.mode === b.mode ? "on" : ""}">${b.t}<small>${b.s}</small></button>`).join("")}
      <button id="play" class="ghost">${S.auto ? "Pause" : "Resume"}<small>one transaction per second</small></button>
    </div>
    <div class="sec-title">Rolling window · last 10 transaction-linked observations <span class="pill neutral" id="wstat">0 / 10</span></div>
    <div class="window" id="window"></div>
    <div class="sec-title">Checks on the window</div>
    <div class="checks" id="checks"></div>
    <div id="alertbox"></div>
    <div class="sec-title">Member's transactions on the live chain</div>
    <div class="txs" id="txs"></div>`;
  P.querySelectorAll(".behave button[data-mode]").forEach((b) => b.onclick = () => {
    S.mode = b.dataset.mode;
    P.querySelectorAll(".behave button[data-mode]").forEach((x) => x.classList.toggle("on", x === b));
    if (S.flagged) return;
    if (!S.auto) setAuto(true);
    addLocal(`Member switched behaviour: ${BEHAVE.find((x) => x.mode === S.mode).t}`, S.mode === "ordinary" ? "" : "bad");
  });
  $("#play").onclick = () => setAuto(!S.auto);
  drawLayer2();
}

function signal(o) {
  return o.conflicting_spends > 0 || (o.competing_tips > 0 && o.clustered_arrivals > 0)
    || (o.total_blocks && o.member_blocks / o.total_blocks > 0.5)
    || (o.expected_pool_blocks && o.reported_pool_blocks / o.expected_pool_blocks < 0.2);
}

function checkMetric(k, f) {
  const m = f ? f.metrics : {};
  if (k === "majority") {
    const share = m.block_share ?? (m.total_blocks ? m.member_blocks / m.total_blocks : null);
    return { text: share === null ? "collecting blocks…" : `${pct(share)} of ${m.total_blocks} accepted blocks`, v: share, limit: 0.5 };
  }
  if (k === "double_spend") return { text: m.conflicting_spends === undefined ? "watching spends…" : `${m.conflicting_spends} conflicting spend(s) · ${m.reversed_confirmations ?? 0} reversal(s)`, v: m.conflicting_spends ? 1 : 0, limit: 0.5 };
  if (k === "selfish_mining") return { text: m.coincident_samples === undefined ? "watching forks…" : `${m.coincident_samples} sample(s) with competing tips + bursts`, v: m.coincident_samples === undefined ? null : Math.min(1, m.coincident_samples / 4), limit: 0.5 };
  const ratio = m.yield_ratio ?? (m.expected_pool_blocks ? m.reported_pool_blocks / m.expected_pool_blocks : null);
  return { text: ratio === null ? "watching pool yield…" : `pool yield ${pct(ratio)} of expected`, v: ratio, limit: 0.2, invert: true };
}

function drawLayer2() {
  if (!$("#window")) return;
  const slots = [];
  for (let i = 0; i < 10; i++) {
    const o = S.slots[S.slots.length - 10 + i] || (S.slots.length < 10 ? S.slots[i] : null);
    slots.push(o);
  }
  const view = S.slots.length >= 10 ? S.slots.slice(-10) : [...S.slots, ...Array(10 - S.slots.length).fill(null)];
  $("#window").innerHTML = view.map((o) => o ? `<div class="slot on ${signal(o) ? "sig" : ""}" title="${esc(o.observation_id)}">#${o.seq}</div>` : '<div class="slot">·</div>').join("");
  const report = S.mon;
  const st = $("#wstat");
  if (!report || report.status === "observing") { st.className = "pill neutral"; st.textContent = `${Math.min(S.count, 10)} / 10 · filling`; }
  else if (report.status === "review") { st.className = "pill bad"; st.textContent = "indicator raised"; }
  else if (report.status === "clear") { st.className = "pill good"; st.textContent = "all clear"; }
  else { st.className = "pill warn"; st.textContent = report.status; }

  $("#checks").innerHTML = Object.keys(CHECK).map((k) => {
    const f = report && report.findings ? report.findings[k] : null;
    const status = f ? f.status : "waiting";
    const mt = checkMetric(k, f);
    const pill = status === "indicator" ? '<span class="pill bad">indicator</span>'
      : status === "clear" ? '<span class="pill good">clear</span>'
      : status === "insufficient" ? '<span class="pill warn">not enough data</span>' : '<span class="pill neutral">waiting</span>';
    const bar = mt.v === null || mt.v === undefined ? 0 : Math.min(1, mt.v);
    const barColor = status === "indicator" ? "var(--red)" : "var(--green)";
    return `<div class="ck ${status}"><div class="t">${CHECK[k].t}${pill}</div>
      <div class="m">${mt.text}<div class="meter"><i style="width:${bar * 100}%;background:${barColor}"></i><u style="left:${mt.limit * 100}%"></u></div></div>
      <div class="rule">${CHECK[k].rule}</div></div>`;
  }).join("");

  $("#txs").innerHTML = S.txs.slice(0, 6).map((t) => `
    <div class="tx ${t.status.startsWith("rejected") ? "rej" : ""}">
      <span><b>nonce ${t.nonce}</b></span>
      <span class="mono">${t.amount} units → ${t.recipient.slice(0, 10)}… · sig ${t.signature.slice(0, 10)}…</span>
      ${t.status === "accepted" ? '<span class="pill good">in block</span>' : '<span class="pill bad" title="' + esc(t.status) + '">refused by replicas</span>'}
    </div>`).join("") || '<div class="sub">Waiting for the first transaction…</div>';

  if (report && report.status === "review") drawAlert(report);
}

function drawAlert(report) {
  const box = $("#alertbox");
  if (!box || box.dataset.on) return;
  box.dataset.on = "1";
  const hits = Object.entries(report.findings).filter(([, f]) => f.status === "indicator");
  box.innerHTML = `<div class="alert">
      <div class="h">⚠ Layer 2 raised an indicator: ${hits.map(([k]) => CHECK[k].t).join(", ")}</div>
      <p>${hits.map(([, f]) => esc(f.explanation)).join(" ")}</p>
      <input type="text" id="rnote" value="${esc(`${hits.map(([k]) => CHECK[k].t).join(", ")} indicator in the rolling window (${hits.flatMap(([, f]) => f.observation_ids).slice(-2).join(", ")}).`)}">
      <div class="row end" style="margin-top:10px">
        <span class="sub" style="margin-right:auto">Revocation is the administrator's call — never automatic.</span>
        <button class="ghost" id="dismiss">Keep watching</button>
        <button class="bad" id="revoke">Revoke membership</button>
      </div></div>`;
  $("#revoke").onclick = revoke;
  $("#dismiss").onclick = () => { box.innerHTML = ""; delete box.dataset.on; S.flagged = false; S.mode = "ordinary"; panelLayer2(); setAuto(true); drawNetwork(); };
  box.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

async function tick() {
  if (S.busy || !S.member) return;
  S.busy = true;
  try {
    const r = await api(`/api/members/${S.member}/tick`, { mode: S.mode });
    S.count = r.count;
    S.slots.push({ ...r.observation, seq: r.count });
    r.transactions.slice().reverse().forEach((t) => S.txs.unshift(t));
    S.mon = r.monitoring;
    S.state.ledger = r.ledger;
    // the transaction goes from the member to the block producer, then the block spreads
    const bad = r.transactions.length > 1;
    await animatePacket(S.member, "relay-0", bad ? "bad" : "tx", 380);
    drawNetwork(); drawLedger();
    propagateBlock();
    if (bad) addLocal(`Replicas refused a second spend of nonce ${r.transactions[1].nonce} (${r.transactions[1].status.replace("rejected: ", "")})`, "bad");
    if (r.monitoring.status === "review" && !S.flagged) {
      S.flagged = true;
      setAuto(false);
      setStep("outcome", true);
      addLocal(`Layer 2 indicator on node ${short(S.member)}: ${Object.entries(r.monitoring.findings).filter(([, f]) => f.status === "indicator").map(([k]) => CHECK[k].t).join(", ")}`, "bad");
      drawNetwork();
    }
    drawLayer2();
  } catch (err) {
    setAuto(false);
    toast(err.message);
  } finally { S.busy = false; }
}

async function revoke() {
  const note = $("#rnote").value.trim();
  if (!note) { toast("Write a reason first"); return; }
  setAuto(false);
  try {
    const id = S.member;
    const edges = S.state.network.edges.filter((e) => e.source === id || e.target === id);
    await api(`/api/members/${id}/revoke`, { note });
    const c = await api(`/api/candidates/${id}`);
    addEvents(c.events, id);
    S.leaving = { id, kind: "member", edges };
    await refresh();
    await sleep(1400);
    S.leaving = null;
    S.member = null;
    await refresh();
    panelOutcome("revoked", id, c);
  } catch (err) { toast(err.message); }
}

/* ---------- outcomes ---------- */
function panelOutcome(kind, id, c) {
  setStep(kind === "revoked" ? "outcome" : "decision");
  const P = $("#panel");
  if (kind === "rejected") {
    P.innerHTML = `<div class="outcome"><div class="badge bad">✕</div>
      <h2>Node kept out of the network</h2>
      <p>The candidate never touched the ledger. Its report, the administrator's reason and every state change stay in the audit trail.</p>
      <button class="primary" id="again">Try another node</button></div>`;
  } else {
    const hits = c && c.events ? c.events.find((e) => e.status === "REVOKED") : null;
    P.innerHTML = `<div class="outcome"><div class="badge bad">⛔</div>
      <h2>Membership revoked</h2>
      <p>The node has left the topology and no longer holds a replica. Its history stays readable for the audit.</p>
      <div class="summary">
        <div><span>Admitted with Trust Factor</span><b>${fx(c && c.factor)}</b></div>
        <div><span>Observations monitored</span><b>${S.count}</b></div>
        <div><span>Validating replicas now</span><b>${S.state.ledger.replicas}</b></div>
        <div><span>Reason</span><b style="max-width:60%;text-align:right">${esc(hits ? hits.detail.note : "")}</b></div>
      </div>
      <button class="primary" id="again">Admit another node</button></div>`;
  }
  $("#again").onclick = panelNetwork;
}

/* ---------- boot ---------- */
$("#reset").onclick = async () => {
  setAuto(false);
  await api("/api/reset", {});
  Object.assign(S, { current: null, cand: null, member: null, flagged: false, leaving: null, lastHeight: null });
  S.log.clear(); drawLog();
  await refresh();
  panelNetwork();
  toast("Demo reset — fresh network");
};

(async () => {
  await refresh();
  drawLog();
  panelNetwork();
  setInterval(() => { if (!S.auto && !S.busy && S.stage !== "layer1") refresh().catch(() => {}); }, 8000);
})();
