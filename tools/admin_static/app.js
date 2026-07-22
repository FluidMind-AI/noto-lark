/* ══════════════════════════════════════════════════════════════════════
   Noto Control Center — SPA (vanilla JS, no dependencies)
   Phase 1: auth, shell, nav, command palette, toast/undo engine,
   Admin workspace (users / sessions / audit).
   Later phases register real views into VIEWS.
   ══════════════════════════════════════════════════════════════════════ */
"use strict";

/* ── tiny DOM helpers ─────────────────────────────────────────────── */
const $ = (sel, el = document) => el.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
function h(html) {
  const t = document.createElement("template");
  t.innerHTML = html.trim();
  return t.content.firstElementChild;
}
/* Multi-root HTML (e.g. `<h3>…</h3><table>…`) — h() keeps only the
   FIRST element; frag() keeps everything. Use for drawer sections. */
function frag(html) {
  const t = document.createElement("template");
  t.innerHTML = html.trim();
  return t.content;
}
const fmtAgo = (epoch) => {
  if (!epoch) return "never";
  const s = Math.max(0, Date.now() / 1000 - epoch);
  if (s < 60) return `${s | 0}s ago`;
  if (s < 3600) return `${(s / 60) | 0}m ago`;
  if (s < 86400) return `${(s / 3600) | 0}h ago`;
  return `${(s / 86400) | 0}d ago`;
};
const fmtTs = (epoch) => epoch
  ? new Date(epoch * 1000).toLocaleString("en-SG",
      { dateStyle: "medium", timeStyle: "short" })
  : "—";
/* Accepts epoch seconds OR ISO strings (stores mix both). */
const anyTs = (v) => {
  if (v == null || v === "") return 0;
  if (typeof v === "number") return v;
  const n = Number(v);
  if (!Number.isNaN(n) && n > 1e8) return n;
  const d = new Date(v);
  return Number.isNaN(d.getTime()) ? 0 : d.getTime() / 1000;
};

/* ── state ────────────────────────────────────────────────────────── */
const state = {
  me: null,          // {open_id, name, role, csrf}
  counts: {},        // inbox badge counts
  countsAt: 0,
  viewKeys: null,    // active view's keyboard handler (returns true = consumed)
};

/* ── API layer ────────────────────────────────────────────────────── */
async function api(path, { method = "GET", body } = {}) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  if (method !== "GET" && state.me) {
    opts.headers["X-Noto-CSRF"] = state.me.csrf;
  }
  const res = await fetch(path, opts);
  if (res.status === 401) { state.me = null; renderLogin(); throw new Error("signed out"); }
  const data = await res.json().catch(() => ({}));
  if (!res.ok || data.ok === false) {
    throw new Error(data.error || `HTTP ${res.status}`);
  }
  return data;
}

/* ── toast + deferred-commit engine ───────────────────────────────────
   queueAction({label, url, body, grace, onCommit, onUndo}) shows an
   undo toast for `grace` ms, then POSTs. Undo cancels the POST — nothing
   is ever reverted server-side (design doc §"undo model"). Pending
   actions flush via sendBeacon on pagehide so closing the tab commits
   rather than silently dropping work.                                  */
const pendingActions = new Set();

function toastHost() {
  let el = $(".toasts");
  if (!el) { el = h('<div class="toasts"></div>'); document.body.appendChild(el); }
  return el;
}

function notify(msg, kind = "", ms = 3200) {
  const el = h(`<div class="toast ${kind}"><span>${esc(msg)}</span></div>`);
  toastHost().appendChild(el);
  setTimeout(() => el.remove(), ms);
}

function queueAction({ label, url, body, grace = 3500, onCommit, onUndo }) {
  const R = 8, C = 2 * Math.PI * R;
  const el = h(`
    <div class="toast">
      <svg class="ring" viewBox="0 0 20 20">
        <circle class="track" cx="10" cy="10" r="${R}" fill="none" stroke-width="2.5"/>
        <circle class="arc" cx="10" cy="10" r="${R}" fill="none" stroke-width="2.5"
                stroke-dasharray="${C}" stroke-dashoffset="0"/>
      </svg>
      <span>${esc(label)}</span>
      <button class="undo">Undo</button>
    </div>`);
  toastHost().appendChild(el);
  const arc = $(".arc", el);
  const t0 = Date.now();
  const action = {
    url, body,
    commit: async () => {
      pendingActions.delete(action);
      clearInterval(tick); el.remove();
      try {
        const res = await api(url, { method: "POST", body });
        onCommit && onCommit(res);
      } catch (e) {
        notify(`Failed: ${e.message}`, "bad", 6000);
        onUndo && onUndo();     // put the row back — the action didn't land
      }
    },
    cancel: () => {
      pendingActions.delete(action);
      clearInterval(tick); el.remove();
      onUndo && onUndo();
    },
  };
  pendingActions.add(action);
  const tick = setInterval(() => {
    const p = Math.min(1, (Date.now() - t0) / grace);
    arc.style.strokeDashoffset = String(C * p);
    if (p >= 1) action.commit();
  }, 100);
  $(".undo", el).onclick = action.cancel;
  return action;
}

window.addEventListener("pagehide", () => {
  // Flush queued actions so closing the tab commits them. sendBeacon
  // can't set headers, so the CSRF token rides in the JSON body.
  for (const a of pendingActions) {
    const payload = { ...(a.body || {}), _csrf: state.me?.csrf };
    navigator.sendBeacon(a.url,
      new Blob([JSON.stringify(payload)], { type: "application/json" }));
  }
  pendingActions.clear();
});

/* ── views registry ───────────────────────────────────────────────── */
const ICONS = {
  inbox: '<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 12h-6l-2 3h-4l-2-3H2"/><path d="M5.5 5.1 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.5-6.9A2 2 0 0 0 16.7 4H7.3a2 2 0 0 0-1.8 1.1z"/></svg>',
  health: '<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 12h-4l-3 9L9 3l-3 9H2"/></svg>',
  usecases: '<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/></svg>',
  playbook: '<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/></svg>',
  admin: '<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>',
};

function comingSoon(phase, blurb) {
  return (el) => {
    el.appendChild(h(`
      <div class="panel"><div class="empty-state">
        <div class="big">◌</div>
        <div><b>Ships in Phase ${phase}.</b></div>
        <div style="margin-top:6px">${esc(blurb)}</div>
      </div></div>`));
  };
}

const VIEWS = {
  inbox: {
    title: "Inbox", icon: "inbox", key: "i",
    badge: () => (state.counts.lessons ?? 0) + (state.counts.rules ?? 0) + (state.counts.nuggets ?? 0),
    render: (el) => renderInbox(el),
  },
  usecases: {
    title: "Use cases", icon: "usecases", key: "u",
    render: (el) => renderUseCases(el),
  },
  health: {
    title: "Health", icon: "health", key: "h",
    render: (el) => renderHealth(el),
  },
  playbook: {
    title: "Playbook", icon: "playbook", key: "b",
    badge: () => state.counts.playbook ?? 0,
    render: (el) => renderPlaybook(el),
  },
  admin: {
    title: "Admin", icon: "admin", key: "a", superOnly: true,
    render: renderAdmin,
  },
};

/* ── router ───────────────────────────────────────────────────────── */
function currentRoute() {
  const name = (location.hash || "#/inbox").replace(/^#\//, "").split("/")[0];
  return VIEWS[name] ? name : "inbox";
}
window.addEventListener("hashchange", () => state.me && renderShell());

function nav(name) { location.hash = `#/${name}`; }

/* ── login ────────────────────────────────────────────────────────── */
function renderLogin() {
  const app = $("#app");
  app.dataset.view = "login";
  app.replaceChildren(h(`
    <div class="login-wrap"><div class="login-card fade-in">
      <div class="login-mark">N</div>
      <h1>Noto Control Center</h1>
      <p class="sub">Sign in with your Lark identity — a one-time link
        will be DM'd to you.</p>
      <label for="oid">Lark open_id</label>
      <div class="row">
        <input class="input mono" id="oid" placeholder="ou_…" autocomplete="off" spellcheck="false">
        <button class="btn primary" id="send">Send link</button>
      </div>
      <div class="login-msg" id="msg"></div>
      <details class="login-alt">
        <summary>Local admin — shared key</summary>
        <div class="row">
          <input class="input mono" id="key" type="password" placeholder="dashboard key">
          <button class="btn" id="keygo">Enter</button>
        </div>
      </details>
    </div></div>`));

  const msg = $("#msg");
  $("#send").onclick = async () => {
    const open_id = $("#oid").value.trim();
    if (!open_id) { msg.textContent = "Enter your open_id."; msg.className = "login-msg bad"; return; }
    $("#send").disabled = true;
    try {
      const r = await fetch("/admin/api/auth/request-link", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ open_id }) });
      const d = await r.json();
      msg.textContent = d.message || d.error || "Sent.";
      msg.className = "login-msg " + (r.ok ? "ok" : "bad");
    } catch (e) { msg.textContent = String(e); msg.className = "login-msg bad"; }
    $("#send").disabled = false;
  };
  $("#oid").addEventListener("keydown", (e) => e.key === "Enter" && $("#send").click());
  $("#keygo").onclick = async () => {
    try {
      const r = await fetch("/admin/api/auth/key", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key: $("#key").value }) });
      const d = await r.json();
      if (r.ok && d.ok) { /* ── Playbook: the house email-response playbook review seat ──────── */
function renderPlaybook(el) {
  el.appendChild(h(`<div class="page-head">
    <h1>Playbook</h1>
    <span class="sub">how VP answers emails — mined nightly from sent mail; active entries are canon for auto-drafts</span>
  </div>`));
  const bar = h(`<div class="panel" style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:10px">
    <select id="pb-status">
      <option value="unreviewed">Unreviewed</option>
      <option value="active">Active (all)</option>
      <option value="disabled">Retired</option>
      <option value="all">Everything</option>
    </select>
    <select id="pb-type"><option value="">All situations</option></select>
    <select id="pb-source"><option value="">All sources</option></select>
    <input id="pb-q" placeholder="search…" style="flex:1;min-width:140px">
    <span id="pb-count" class="sub"></span>
  </div>`);
  el.appendChild(bar);
  const root = h('<div><div class="empty-state"><span class="spin"></span></div></div>');
  el.appendChild(root);

  async function load() {
    const q = new URLSearchParams();
    q.set("status", bar.querySelector("#pb-status").value);
    const t = bar.querySelector("#pb-type").value; if (t) q.set("type", t);
    const src = bar.querySelector("#pb-source").value; if (src) q.set("source", src);
    const s = bar.querySelector("#pb-q").value.trim(); if (s) q.set("q", s);
    let d;
    try { d = await api("/admin/api/playbook?" + q.toString()); }
    catch (e) { notify(e.message, "bad", 5000); return; }
    const typeSel = bar.querySelector("#pb-type");
    if (typeSel.options.length <= 1 && d.stats?.by_type) {
      Object.entries(d.stats.by_type).forEach(([ty, n]) =>
        typeSel.appendChild(h(`<option value="${ty}">${ty} (${n})</option>`)));
    }
    const srcSel = bar.querySelector("#pb-source");
    if (srcSel.options.length <= 1 && d.stats?.by_user) {
      Object.keys(d.stats.by_user).forEach(u =>
        srcSel.appendChild(h(`<option value="${u}">${u}</option>`)));
    }
    bar.querySelector("#pb-count").textContent =
      `${(d.entries || []).length} shown · ${d.stats?.entries ?? "?"} total`;
    if (!(d.entries || []).length) {
      root.replaceChildren(h(`<div class="panel"><div class="empty-state">
        <div class="big">◌</div><div><b>Nothing here.</b></div>
        <div style="margin-top:6px">The nightly miner adds entries at 01:30 —
        or switch the filter above.</div></div></div>`));
      return;
    }
    root.replaceChildren(...d.entries.map(card));
  }

  function card(e) {
    const conf = e.status === "active"
      ? (e.reviewed_at ? `<span class="count">kept ${e.reviewed_at.slice(0,10)}</span>` : "")
      : `<span class="count hot">retired</span>`;
    const c = h(`<div class="panel" style="margin-bottom:10px">
      <div style="display:flex;gap:8px;align-items:baseline;flex-wrap:wrap">
        <b>#${e.id}</b>
        <span class="count">${e.situation_type}</span>
        <span class="sub">${e.source_user} · ${e.sent_date || "?"} · authority ${e.authority}</span>
        ${conf}
      </div>
      <div style="margin:8px 0 4px"><b>Situation:</b> ${esc(e.situation)}</div>
      <div style="margin:4px 0"><b>Approach:</b> ${esc(e.approach)}</div>
      <div style="margin:4px 0" class="sub"><b>Tone:</b> ${esc(e.tone || "—")}</div>
      <details style="margin:6px 0"><summary>Exemplar (the actual reply)</summary>
        <pre style="white-space:pre-wrap;font-size:12px;margin:6px 0">${esc(e.exemplar || "")}</pre></details>
      <details class="pb-prov" style="margin:6px 0"><summary>Provenance — the exchange it was mined from</summary>
        <div class="pb-prov-body sub">loading…</div></details>
      <div style="display:flex;gap:8px;margin-top:8px">
        <button class="btn pb-keep">✓ Keep</button>
        <button class="btn danger pb-disable">Retire</button>
      </div>
    </div>`);
    c.querySelector(".pb-prov").addEventListener("toggle", async (ev) => {
      const box = c.querySelector(".pb-prov-body");
      if (!ev.target.open || box.dataset.loaded) return;
      try {
        const d = await api(`/admin/api/playbook/${e.id}/provenance`);
        box.dataset.loaded = "1";
        box.replaceChildren(h(`<div>
          <div style="margin:6px 0"><b>They wrote${d.inbound ? " (" + esc(d.inbound.from || "") + ")" : ""}:</b>
            <pre style="white-space:pre-wrap;font-size:12px">${esc(d.inbound?.body || "(no inbound found — outbound-initiated)")}</pre></div>
          <div style="margin:6px 0"><b>${esc(e.source_user)} replied:</b>
            <pre style="white-space:pre-wrap;font-size:12px">${esc(d.sent?.body || "(not in the mirror)")}</pre></div>
          <div class="sub">Noto read this exchange and distilled the Situation/Approach/Tone above; the reply itself became the exemplar.</div>
        </div>`));
      } catch (err) { box.textContent = "provenance failed: " + err.message; }
    });
    c.querySelector(".pb-keep").onclick = () =>
      act(e.id, "keep", `#${e.id} kept as canon`);
    c.querySelector(".pb-disable").onclick = () =>
      act(e.id, "disable", `#${e.id} retired from drafting`);
    async function act(id, what, label) {
      try {
        await api(`/admin/api/playbook/${id}/${what}`, { method: "POST", body: {} });
        notify(label, "ok");
        load();
      } catch (err) { notify(err.message, "bad", 5000); }
    }
    return c;
  }

  function esc(t) {
    return String(t ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }
  ["#pb-status", "#pb-type", "#pb-source"].forEach(sel =>
    bar.querySelector(sel).onchange = load);
  let deb;
  bar.querySelector("#pb-q").oninput = () => { clearTimeout(deb); deb = setTimeout(load, 350); };
  load();
}

boot(); }
      else { msg.textContent = d.error || "Invalid key."; msg.className = "login-msg bad"; }
    } catch (e) { msg.textContent = String(e); msg.className = "login-msg bad"; }
  };
  $("#key").addEventListener("keydown", (e) => e.key === "Enter" && $("#keygo").click());
  $("#oid").focus();
}

/* ── shell ────────────────────────────────────────────────────────── */
function renderShell() {
  const app = $("#app");
  const route = currentRoute();
  const view = VIEWS[route];
  if (view.superOnly && state.me.role !== "super_admin") { nav("inbox"); return; }
  app.dataset.view = route;

  const navItems = Object.entries(VIEWS)
    .filter(([, v]) => !v.superOnly || state.me.role === "super_admin")
    .map(([name, v]) => {
      const n = v.badge ? v.badge() : 0;
      return `
        <button class="nav-item ${name === route ? "active" : ""}" data-nav="${name}">
          ${ICONS[v.icon]}<span>${v.title}</span>
          ${n ? `<span class="count ${name === "inbox" ? "hot" : ""}">${n}</span>` : ""}
        </button>`;
    }).join("");

  const initials = (state.me.name || "?").split(/\s+/).map(w => w[0]).join("").slice(0, 2).toUpperCase();

  app.replaceChildren(h(`
    <div class="shell">
      <aside class="side">
        <div class="side-brand">
          <div class="mark">N</div>
          <div><div class="name">Noto</div><div class="env">control center</div></div>
        </div>
        ${navItems}
        <div class="side-foot">
          <div class="avatar">${esc(initials)}</div>
          <div class="who">
            <div class="nm">${esc(state.me.name || state.me.open_id)}</div>
            <div class="rl">${esc(state.me.role)}</div>
          </div>
          <button class="out" id="logout" title="Sign out">⏻</button>
        </div>
      </aside>
      <div class="main">
        <div class="topbar">
          <span class="crumb">${esc(view.title)}</span>
          <button class="searchbtn" id="palette-open">
            <span>Search or jump to…</span><span class="spacer"></span><kbd>⌘K</kbd>
          </button>
          <span class="live-dot" id="livedot" title="counts refresh every 30s"></span>
        </div>
        <div class="content"><div class="content-inner fade-in" id="view"></div></div>
      </div>
    </div>`));

  app.querySelectorAll("[data-nav]").forEach(b => b.onclick = () => nav(b.dataset.nav));
  $("#logout").onclick = async () => {
    try { await api("/admin/api/auth/logout", { method: "POST", body: {} }); } catch {}
    state.me = null; renderLogin();
  };
  $("#palette-open").onclick = openPalette;

  state.viewKeys = null;          // views opt back in via state.viewKeys
  view.render($("#view"));
}

/* ── counts polling ───────────────────────────────────────────────── */
async function refreshCounts() {
  if (!state.me) return;
  try {
    const d = await api("/admin/api/inbox/counts");
    state.counts = d; state.countsAt = Date.now();
    // update badges in place without a full re-render
    document.querySelectorAll("[data-nav]").forEach(btn => {
      const v = VIEWS[btn.dataset.nav];
      const n = v.badge ? v.badge() : 0;
      let c = btn.querySelector(".count");
      if (n && !c) {
        c = h(`<span class="count ${btn.dataset.nav === "inbox" ? "hot" : ""}">${n}</span>`);
        btn.appendChild(c);
      } else if (c) { n ? (c.textContent = n) : c.remove(); }
    });
    $("#livedot")?.classList.remove("stale");
  } catch { $("#livedot")?.classList.add("stale"); }
}
setInterval(refreshCounts, 30000);

/* ── command palette ──────────────────────────────────────────────── */
let paletteEl = null;
function openPalette() {
  if (paletteEl) return;
  const items = Object.entries(VIEWS)
    .filter(([, v]) => !v.superOnly || state.me.role === "super_admin")
    .map(([name, v]) => ({ label: v.title, hint: `g ${v.key}`, run: () => nav(name) }));
  paletteEl = h(`
    <div class="palette-overlay"><div class="palette">
      <input placeholder="Jump to a workspace…">
      <div class="results"></div>
    </div></div>`);
  document.body.appendChild(paletteEl);
  const input = $("input", paletteEl);
  const results = $(".results", paletteEl);
  let cursor = 0, shown = items;

  function paint() {
    const blocks = [];
    if (shown.length) {
      blocks.push(h(`<div class="group">Workspaces</div>`));
      shown.forEach((it, i) => {
        const el = h(`<div class="item ${i === cursor ? "cursor" : ""}">
          <span>${esc(it.label)}</span><span class="k"><kbd>${esc(it.hint)}</kbd></span></div>`);
        el.onclick = () => { close(); it.run(); };
        blocks.push(el);
      });
    }
    results.replaceChildren(...blocks);
    if (!blocks.length) results.replaceChildren(h(`<div class="empty-state">No matches</div>`));
  }
  function close() { paletteEl.remove(); paletteEl = null; }

  input.oninput = () => {
    const q = input.value.trim().toLowerCase();
    shown = items.filter(it => it.label.toLowerCase().includes(q));
    cursor = 0; paint();
  };
  input.onkeydown = (e) => {
    if (e.key === "Escape") close();
    else if (e.key === "ArrowDown") { cursor = Math.min(cursor + 1, shown.length - 1); paint(); }
    else if (e.key === "ArrowUp") { cursor = Math.max(cursor - 1, 0); paint(); }
    else if (e.key === "Enter" && shown[cursor]) { close(); shown[cursor].run(); }
  };
  paletteEl.onclick = (e) => { if (e.target === paletteEl) close(); };
  paint(); input.focus();
}

/* ── keyboard ─────────────────────────────────────────────────────── */
let gPending = false, helpEl = null;
document.addEventListener("keydown", (e) => {
  if (!state.me) return;
  const inField = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName || "");
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
    e.preventDefault(); paletteEl ? null : openPalette(); return;
  }
  if (inField || paletteEl) return;
  if (e.key === "Escape" && helpEl) { helpEl.remove(); helpEl = null; return; }
  if (!gPending && state.viewKeys && state.viewKeys(e)) { e.preventDefault(); return; }
  if (gPending) {
    gPending = false;
    const hit = Object.entries(VIEWS).find(([, v]) => v.key === e.key.toLowerCase());
    if (hit && (!hit[1].superOnly || state.me.role === "super_admin")) { e.preventDefault(); nav(hit[0]); }
    return;
  }
  if (e.key === "g") { gPending = true; setTimeout(() => gPending = false, 900); return; }
  if (e.key === "?") { toggleHelp(); }
});

function toggleHelp() {
  if (helpEl) { helpEl.remove(); helpEl = null; return; }
  const rows = [
    ["⌘K", "command palette"], ["g i", "Inbox"],
    ["g u", "Use cases"], ["g h", "Health"],
    ["g a", "Admin"], ["j / k", "move row cursor"], ["x", "select row"],
    ["a", "approve"], ["r", "reject / dismiss"], ["e", "edit"],
    ["⏎", "open detail"], ["/", "focus filter"], ["Z", "undo last"],
    ["?", "this overlay"], ["Esc", "close"],
  ];
  helpEl = h(`
    <div class="help-overlay"><div class="help-card">
      <h2>Keyboard shortcuts</h2>
      <div class="help-grid">
        ${rows.map(([k, d]) => `<div class="hk"><kbd>${esc(k)}</kbd><span>${esc(d)}</span></div>`).join("")}
      </div>
    </div></div>`);
  helpEl.onclick = (e) => { if (e.target === helpEl) { helpEl.remove(); helpEl = null; } };
  document.body.appendChild(helpEl);
}

/* ══ ADMIN WORKSPACE ══════════════════════════════════════════════════ */
async function renderAdmin(el) {
  el.appendChild(h(`<div class="page-head">
    <h1>Admin</h1><span class="sub">panel users · sessions · audit</span>
  </div>`));

  /* users */
  const usersPanel = h(`
    <div class="panel">
      <div class="panel-head"><h2>Panel users</h2>
        <div class="actions"><button class="btn sm primary" id="add-user">Add user</button></div>
      </div>
      <div class="panel-body flush"><table class="grid"><thead><tr>
        <th>User</th><th>open_id</th><th>Role</th><th>Added</th><th>Status</th><th></th>
      </tr></thead><tbody id="users-body"></tbody></table></div>
    </div>`);
  el.appendChild(usersPanel);

  const addForm = h(`
    <div class="panel" style="display:none" id="add-form">
      <div class="panel-head"><h2>Add / update user</h2></div>
      <div class="panel-body"><div class="row" style="flex-wrap:wrap">
        <input class="input mono" id="nu-oid" placeholder="ou_…" style="flex:2;min-width:260px">
        <input class="input" id="nu-name" placeholder="Display name" style="flex:1;min-width:140px">
        <select class="input" id="nu-role" style="width:140px">
          <option value="member">member</option>
          <option value="super_admin">super_admin</option>
        </select>
        <button class="btn primary" id="nu-save">Save</button>
      </div><div class="login-msg" id="nu-msg"></div></div>
    </div>`);
  usersPanel.after(addForm);
  $("#add-user").onclick = () => {
    addForm.style.display = addForm.style.display === "none" ? "" : "none";
    $("#nu-oid").focus();
  };

  async function loadUsers() {
    const d = await api("/admin/api/admin/users");
    $("#users-body").replaceChildren(...d.users.map(u => {
      const tr = h(`<tr>
        <td><b>${esc(u.name || "—")}</b></td>
        <td class="mono muted">${esc(u.open_id)}</td>
        <td>${u.role === "super_admin"
          ? '<span class="pill acc">super_admin</span>'
          : '<span class="pill dim">member</span>'}</td>
        <td class="muted nowrap">${fmtAgo(u.added_at)}${u.added_by ? ` · by ${esc(u.added_by === "(seed)" ? "seed" : u.added_by.slice(-6))}` : ""}</td>
        <td>${u.disabled ? '<span class="pill bad">disabled</span>' : '<span class="pill ok">active</span>'}</td>
        <td><div class="rowactions">
          <button class="btn sm ghost" data-act="edit">Edit</button>
          <button class="btn sm ghost ${u.disabled ? "ok" : "danger"}" data-act="toggle">
            ${u.disabled ? "Enable" : "Disable"}</button>
        </div></td>
      </tr>`);
      tr.querySelector('[data-act="edit"]').onclick = () => {
        addForm.style.display = "";
        $("#nu-oid").value = u.open_id;
        $("#nu-name").value = u.name || "";
        $("#nu-role").value = u.role;
        $("#nu-name").focus();
      };
      tr.querySelector('[data-act="toggle"]').onclick = async () => {
        try {
          await api("/admin/api/admin/users", { method: "POST",
            body: { open_id: u.open_id, name: u.name, role: u.role, disabled: !u.disabled } });
          notify(`${u.name || u.open_id} ${u.disabled ? "enabled" : "disabled"}`, "ok");
          loadUsers();
        } catch (e) { notify(e.message, "bad", 5000); }
      };
      return tr;
    }));
  }
  $("#nu-save").onclick = async () => {
    const m = $("#nu-msg");
    try {
      await api("/admin/api/admin/users", { method: "POST", body: {
        open_id: $("#nu-oid").value.trim(),
        name: $("#nu-name").value.trim(),
        role: $("#nu-role").value } });
      m.textContent = "Saved."; m.className = "login-msg ok";
      $("#nu-oid").value = ""; $("#nu-name").value = "";
      loadUsers();
    } catch (e) { m.textContent = e.message; m.className = "login-msg bad"; }
  };

  /* sessions */
  const sessPanel = h(`
    <div class="panel">
      <div class="panel-head"><h2>Active sessions</h2></div>
      <div class="panel-body flush"><table class="grid"><thead><tr>
        <th>Who</th><th>Via</th><th>Client</th><th>Started</th><th>Last seen</th><th></th>
      </tr></thead><tbody id="sess-body"></tbody></table></div>
    </div>`);
  el.appendChild(sessPanel);

  async function loadSessions() {
    const d = await api("/admin/api/admin/sessions");
    $("#sess-body").replaceChildren(...d.sessions.map(s => {
      const tr = h(`<tr>
        <td><b>${esc(s.open_id)}</b></td>
        <td>${s.via === "shared_key"
          ? '<span class="pill warn">shared key</span>'
          : '<span class="pill dim">magic link</span>'}</td>
        <td class="muted" style="max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(s.user_agent)}</td>
        <td class="muted nowrap">${fmtAgo(s.created_at)}</td>
        <td class="muted nowrap">${fmtAgo(s.last_seen_at)}</td>
        <td><div class="rowactions">
          <button class="btn sm ghost danger">Revoke</button>
        </div></td>
      </tr>`);
      tr.querySelector("button").onclick = async () => {
        try {
          await api("/admin/api/admin/sessions/revoke", { method: "POST", body: { id: s.id } });
          notify("Session revoked", "ok"); loadSessions();
        } catch (e) { notify(e.message, "bad", 5000); }
      };
      return tr;
    }));
    if (!d.sessions.length)
      $("#sess-body").replaceChildren(h('<tr><td colspan="6" class="muted">none</td></tr>'));
  }

  /* audit */
  const auditPanel = h(`
    <div class="panel">
      <div class="panel-head"><h2>Panel audit · last 100</h2></div>
      <div class="panel-body flush"><table class="grid"><thead><tr>
        <th>When</th><th>Actor</th><th>Action</th><th>Target</th><th>Detail</th>
      </tr></thead><tbody id="audit-body"></tbody></table></div>
    </div>`);
  el.appendChild(auditPanel);

  async function loadAudit() {
    const d = await api("/admin/api/admin/audit");
    $("#audit-body").replaceChildren(...d.audit.map(a => h(`<tr>
      <td class="muted nowrap" title="${esc(fmtTs(a.ts))}">${fmtAgo(a.ts)}</td>
      <td><b>${esc(a.actor_name || a.actor_open_id)}</b></td>
      <td class="mono">${esc(a.action)}</td>
      <td class="mono muted">${esc(a.target)}</td>
      <td class="muted" style="max-width:340px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(a.payload_json === "{}" ? "" : a.payload_json)}</td>
    </tr>`)));
    if (!d.audit.length)
      $("#audit-body").replaceChildren(h('<tr><td colspan="5" class="muted">no audit entries yet</td></tr>'));
  }

  try { await Promise.all([loadUsers(), loadSessions(), loadAudit()]); }
  catch (e) { notify(e.message, "bad", 5000); }
}

/* ══ QUEUE ENGINE ═════════════════════════════════════════════════════
   Shared by Rules / Triage / Nuggets: filter bar, row cursor (j/k),
   selection (x), batch bar, inline actions, deferred-commit undo,
   optimistic removal with restore-on-undo.                             */

function Queue(host, cfg) {
  /* cfg: {
       load(filters) -> {rows, facets?}       — fetch
       rowKey(r) -> id
       header: `<th>…</th>` string (checkbox col added automatically)
       renderCells(r) -> `<td>…</td>` string  — data cells only
       actions: [{key, label, kind, batch?, url(r), body(r)->obj,
                  confirmLabel(r|n) -> toast label}]
       filters: [{name, label, type:'select'|'search', options?}]
       editor?: {fields(r) -> [{name,label,value,textarea?}],
                 action, // action name used on save
                 url(r), body(r, values) -> obj}
       emptyText
     }                                                                 */
  let rows = [], cursor = 0, selected = new Set(), filters = {}, facets = {};
  let editingKey = null;

  const root = h(`<div class="queue">
    <div class="row qfilters" style="margin:0 0 12px"></div>
    <div class="panel"><div class="panel-body flush">
      <table class="grid"><thead><tr>
        <th style="width:28px"><input type="checkbox" class="selall"></th>
        ${cfg.header}
        <th class="right" style="width:${(cfg.actions.length * 78) + 60}px"></th>
      </tr></thead><tbody></tbody></table>
    </div></div>
  </div>`);
  host.replaceChildren(root);
  const tbody = $("tbody", root);
  const fbar = $(".qfilters", root);

  /* filter bar */
  for (const f of cfg.filters || []) {
    if (f.type === "select") {
      const sel = h(`<select class="input" style="width:auto" data-f="${f.name}">
        ${(f.options || []).map(o => `<option value="${esc(o.value)}">${esc(o.label)}</option>`).join("")}
      </select>`);
      sel.onchange = () => { filters[f.name] = sel.value; refresh(); };
      filters[f.name] = f.options?.[0]?.value ?? "";
      fbar.appendChild(sel);
    } else {
      const inp = h(`<input class="input qsearch" placeholder="${esc(f.label)}  ( / )" style="max-width:280px">`);
      inp.oninput = () => { filters.__q = inp.value.toLowerCase(); paint(); };
      fbar.appendChild(inp);
    }
  }
  const counter = h('<span class="muted" style="margin-left:auto"></span>');
  fbar.appendChild(counter);

  $(".selall", root).onchange = (e) => {
    selected = e.target.checked ? new Set(visible().map(cfg.rowKey)) : new Set();
    paint();
  };

  function visible() {
    const q = filters.__q;
    return q ? rows.filter(r => JSON.stringify(r).toLowerCase().includes(q)) : rows;
  }

  function updateFacetSelect(name, values, current) {
    const sel = fbar.querySelector(`[data-f="${name}"]`);
    if (!sel) return;
    const keep = current ?? sel.value;
    const base = (cfg.filters.find(f => f.name === name)?.options) || [];
    sel.replaceChildren(
      ...base.map(o => h(`<option value="${esc(o.value)}">${esc(o.label)}</option>`)),
      ...values.map(v => h(`<option value="${esc(v)}">${esc(v)}</option>`)));
    sel.value = keep;
  }

  async function refresh() {
    try {
      const d = await cfg.load(filters);
      rows = d.rows; facets = d.facets || {};
      for (const [name, vals] of Object.entries(facets)) updateFacetSelect(name, vals);
      cursor = Math.min(cursor, Math.max(0, visible().length - 1));
      selected = new Set([...selected].filter(id => rows.some(r => cfg.rowKey(r) === id)));
      paint();
    } catch (e) { notify(e.message, "bad", 5000); }
  }

  function removeRows(ids) {
    const removed = rows.filter(r => ids.includes(cfg.rowKey(r)));
    rows = rows.filter(r => !ids.includes(cfg.rowKey(r)));
    ids.forEach(id => selected.delete(id));
    cursor = Math.min(cursor, Math.max(0, visible().length - 1));
    paint();
    return removed;
  }
  function restoreRows(removed) { rows = removed.concat(rows); paint(); }

  function runAction(action, targets, values) {
    const ids = targets.map(cfg.rowKey);
    const removed = removeRows(ids);
    const label = typeof action.confirmLabel === "function"
      ? action.confirmLabel(targets.length === 1 ? targets[0] : targets.length)
      : `${action.label} ${ids.length} item(s)`;
    if (ids.length === 1) {
      queueAction({
        label, url: action.url(targets[0]),
        body: action.body ? action.body(targets[0], values) : {},
        onCommit: () => { refreshCounts(); },
        onUndo: () => restoreRows(removed),
      });
    } else {
      queueAction({
        label, url: "/admin/api/batch",
        body: { action: action.batch, ids,
                params: action.body ? action.body(null, values) : {} },
        onCommit: (res) => {
          refreshCounts();
          if (res.failed) {
            notify(`${res.failed} of ${ids.length} failed — refreshing`, "bad", 5000);
            refresh();
          }
        },
        onUndo: () => restoreRows(removed),
      });
    }
  }

  function openEditor(r) {
    editingKey = cfg.rowKey(r); paint();
  }

  function paint() {
    const vis = visible();
    counter.textContent = `${vis.length} shown · ${selected.size} selected`;
    /* <details> open-state must survive repaints — paint() rebuilds the
       tbody, which would otherwise slam every expander shut the moment
       the row-click repaint fires. */
    const openKeys = new Set();
    tbody.querySelectorAll("tr[data-key]").forEach(tr => {
      if (tr.querySelector("details[open]")) openKeys.add(tr.dataset.key);
    });
    tbody.replaceChildren(...vis.map((r, i) => {
      const id = cfg.rowKey(r);
      const tr = h(`<tr data-key="${esc(String(id))}" class="${i === cursor ? "cursor" : ""} ${selected.has(id) ? "selected" : ""}">
        <td><input type="checkbox" ${selected.has(id) ? "checked" : ""}></td>
        ${cfg.renderCells(r)}
        <td><div class="rowactions">
          ${cfg.editor ? '<button class="btn sm ghost" data-a="__edit">Edit</button>' : ""}
          ${cfg.actions.map(a => `<button class="btn sm ghost ${a.kind || ""}" data-a="${a.batch}">${a.label}</button>`).join("")}
        </div></td>
      </tr>`);
      if (openKeys.has(String(id)))
        tr.querySelector("details")?.setAttribute("open", "");
      $("input", tr).onchange = (e) => {
        e.target.checked ? selected.add(id) : selected.delete(id);
        cursor = i; paint();
      };
      tr.addEventListener("click", (e) => {
        if (/^(INPUT|BUTTON|SELECT|TEXTAREA|A|SUMMARY|DETAILS)$/.test(e.target.tagName)) return;
        if (e.target.closest("details")) return;   // expander content
        cursor = i; paint();
      });
      tr.querySelectorAll("[data-a]").forEach(btn => btn.onclick = () => {
        if (btn.dataset.a === "__edit") return openEditor(r);
        const a = cfg.actions.find(x => x.batch === btn.dataset.a);
        a.pick ? a.pick(r, (vals) => runAction(a, [r], vals)) : runAction(a, [r]);
      });
      if (cfg.decorateRow) cfg.decorateRow(tr, r, { runAction, refresh });
      return tr;
    }));
    if (!vis.length) {
      tbody.replaceChildren(h(`<tr><td colspan="99"><div class="empty-state">
        <div class="big">✓</div><div>${esc(cfg.emptyText || "Queue is clear.")}</div>
      </div></td></tr>`));
    }
    /* inline editor row */
    if (editingKey != null) {
      const r = rows.find(x => cfg.rowKey(x) === editingKey);
      const anchor = [...tbody.children][vis.findIndex(x => cfg.rowKey(x) === editingKey)];
      if (r && anchor) {
        const fields = cfg.editor.fields(r);
        const ed = h(`<tr class="selected"><td></td><td colspan="98">
          <div style="padding:6px 0 10px">
            ${fields.map(f => `
              <label style="display:block;font-size:11px;color:var(--ink-3);margin:8px 0 4px;text-transform:uppercase;letter-spacing:.05em">${esc(f.label)}</label>
              ${f.textarea
                ? `<textarea class="input" data-ed="${f.name}" rows="3" style="resize:vertical">${esc(f.value)}</textarea>`
                : `<input class="input" data-ed="${f.name}" value="${esc(f.value)}">`}`).join("")}
            <div class="row" style="margin-top:10px">
              <button class="btn primary" data-ed-save>Save &amp; ${esc(cfg.editor.saveLabel)}</button>
              <button class="btn ghost" data-ed-cancel>Cancel</button>
            </div>
          </div></td></tr>`);
        anchor.after(ed);
        $("[data-ed-save]", ed).onclick = () => {
          const values = {};
          ed.querySelectorAll("[data-ed]").forEach(f => values[f.dataset.ed] = f.value);
          editingKey = null;
          runAction(cfg.editor.action, [r], values);
        };
        $("[data-ed-cancel]", ed).onclick = () => { editingKey = null; paint(); };
        $("[data-ed]", ed)?.focus();
      } else { editingKey = null; }
    }
    /* batch bar */
    let bar = $(".batchbar");
    if (selected.size > 1) {
      const html = `<div class="batchbar"><span><span class="n">${selected.size}</span> selected</span>
        ${cfg.actions.filter(a => !a.noBatch).map(a => `<button class="btn sm ${a.kind || ""}" data-b="${a.batch}">${a.label} all</button>`).join("")}
        <button class="btn sm ghost" data-b="__clear">Clear</button></div>`;
      if (bar) bar.replaceWith(bar = h(html)); else document.body.appendChild(bar = h(html));
      bar.querySelectorAll("[data-b]").forEach(btn => btn.onclick = () => {
        if (btn.dataset.b === "__clear") { selected = new Set(); paint(); return; }
        const a = cfg.actions.find(x => x.batch === btn.dataset.b);
        runAction(a, rows.filter(r => selected.has(cfg.rowKey(r))));
      });
    } else if (bar) bar.remove();
  }

  function keyHandler(e) {
    const vis = visible();
    if (!vis.length) return false;
    const cur = vis[cursor];
    switch (e.key) {
      case "j": cursor = Math.min(cursor + 1, vis.length - 1); paint(); scrollCursor(); return true;
      case "k": cursor = Math.max(cursor - 1, 0); paint(); scrollCursor(); return true;
      case "x": {
        const id = cfg.rowKey(cur);
        selected.has(id) ? selected.delete(id) : selected.add(id);
        cursor = Math.min(cursor + 1, vis.length - 1);
        paint(); return true;
      }
      case "e": if (cfg.editor) { openEditor(cur); return true; } return false;
      case "/": $(".qsearch", root)?.focus(); return true;
      default: {
        const a = cfg.actions.find(x => x.key === e.key);
        if (!a) return false;
        const targets = selected.size > 1
          ? rows.filter(r => selected.has(cfg.rowKey(r))) : [cur];
        if (targets.length === 1 && a.pick) a.pick(targets[0], (v) => runAction(a, targets, v));
        else runAction(a, targets);
        return true;
      }
    }
  }
  function scrollCursor() {
    $("tr.cursor", tbody)?.scrollIntoView({ block: "nearest" });
  }

  const cleanup = () => { $(".batchbar")?.remove(); };
  refresh();
  return { refresh, keyHandler, cleanup };
}

/* ══ INBOX WORKSPACE ══════════════════════════════════════════════════ */

let inboxOpenTab = null;   // set by renderInbox; used for cross-tab jumps

const pillKind = (k) => ({
  rule: '<span class="pill acc">rule</span>',
  engineering: '<span class="pill warn">engineering</span>',
  both: '<span class="pill ok">both</span>',
  unsure: '<span class="pill dim">unsure</span>',
}[k] || `<span class="pill dim">${esc(k)}</span>`);

const pillStatus = (s) => ({
  pending: '<span class="pill warn">pending</span>',
  approved: '<span class="pill ok">approved</span>',
  active: '<span class="pill ok">active</span>',
  rejected: '<span class="pill bad">rejected</span>',
  superseded: '<span class="pill dim">superseded</span>',
  deferred: '<span class="pill dim">parked</span>',
  unresolved: '<span class="pill warn">unresolved</span>',
  accepted: '<span class="pill ok">accepted</span>',
}[s] || `<span class="pill dim">${esc(s)}</span>`);

function renderInbox(el) {
  el.appendChild(h(`<div class="page-head">
    <h1>Inbox</h1>
    <span class="sub">Noto derives lessons from feedback — you judge the lessons, with reasoning attached</span>
  </div>`));
  const tabs = h(`<div class="tabs">
    <button class="tab" data-t="lessons">Lessons <span class="count" id="tc-lessons">–</span></button>
    <button class="tab" data-t="triage">Feedback (evidence) <span class="count" id="tc-triage">–</span></button>
    <button class="tab" data-t="rules">Doc-edit rules <span class="count" id="tc-rules">–</span></button>
    <button class="tab" data-t="nuggets">Nuggets <span class="count" id="tc-nuggets">–</span></button>
  </div>`);
  const body = h('<div></div>');
  el.append(tabs, body);

  let active = null;
  function setCounts() {
    $("#tc-lessons").textContent = state.counts.lessons ?? "–";
    $("#tc-rules").textContent = state.counts.rules ?? "–";
    $("#tc-triage").textContent = state.counts.feedback ?? "–";
    $("#tc-nuggets").textContent = state.counts.nuggets ?? "–";
  }
  setCounts();

  const outerKeys = (e) => {
    if (e.key === "1") { open("lessons"); return true; }
    if (e.key === "2") { open("triage"); return true; }
    if (e.key === "3") { open("rules"); return true; }
    if (e.key === "4") { open("nuggets"); return true; }
    return active?.keyHandler ? active.keyHandler(e) : false;
  };
  function open(name) {
    active?.cleanup?.();
    tabs.querySelectorAll(".tab").forEach(t =>
      t.classList.toggle("active", t.dataset.t === name));
    body.replaceChildren();
    active = { lessons: inboxLessons, rules: inboxRules,
               triage: inboxTriage, nuggets: inboxNuggets }[name](body);
    state.viewKeys = outerKeys;
  }
  inboxOpenTab = open;   // lets evidence rows jump to their lesson
  tabs.querySelectorAll(".tab").forEach(t => t.onclick = () => open(t.dataset.t));
  open((state.counts.lessons ?? 0) > 0 ? "lessons"
       : (state.counts.rules ?? 0) > 0 ? "rules"
       : (state.counts.nuggets ?? 0) > 0 ? "nuggets" : "lessons");
}

/* ── Lessons tab — the synthesis review surface ── */
const scopePill = (s) => ({
  global: '<span class="pill acc">global</span>',
  workflow: '<span class="pill ok">workflow</span>',
  engineering: '<span class="pill warn">engineering</span>',
  candidate_specific: '<span class="pill dim">candidate-specific</span>',
  insufficient_evidence: '<span class="pill dim">needs more evidence</span>',
}[s] || `<span class="pill dim">${esc(s)}</span>`);

function inboxLessons(host) {
  const bar = h(`<div class="row" style="margin:0 0 10px">
    <button class="btn primary sm" id="ls-synth">Synthesize now</button>
    <span class="muted" id="ls-note" style="font-size:12px"></span>
  </div>`);
  host.appendChild(bar);

  const q = Queue(host, {
    load: async (f) => {
      const qs = new URLSearchParams();
      if (f.status) qs.set("status", f.status);
      if (f.scope) qs.set("scope", f.scope);
      const d = await api(`/admin/api/lessons?${qs}`);
      const note = $("#ls-note");
      if (note) {
        note.innerHTML = d.synth?.running
          ? '<span class="spin" style="width:11px;height:11px;vertical-align:-2px"></span> Noto is reading the feedback…'
          : `${d.stats?.unsynthesized ?? 0} feedback item(s) not yet synthesized`;
      }
      return { rows: d.lessons };
    },
    rowKey: (r) => r.id,
    header: `<th class="primary">Lesson (Noto's conclusion)</th><th>Scope</th><th class="right">Conf.</th><th>Evidence</th><th>Status</th>`,
    renderCells: (r) => {
      const ev = r.evidence || [];
      const people = [...new Set(ev.map(e => e.user_name || "?"))];
      return `
      <td class="primary">
        <div style="font-weight:600">${esc(r.lesson_text)}</div>
        <details style="margin-top:5px">
          <summary style="cursor:pointer;color:var(--acc);font-size:12px">reasoning &amp; audit trail</summary>
          <div style="margin:8px 0 4px;padding:10px 12px;background:var(--bg0);border:1px solid var(--line);border-radius:7px">
            <div style="font-size:12px;color:var(--ink-2)"><b>Why Noto concluded this:</b> ${esc(r.reasoning || "(no reasoning recorded)")}</div>
            ${ev.map(e => `<div style="margin-top:8px;padding-top:8px;border-top:1px solid var(--line);font-size:12px">
              <span class="pill dim">#${e.id}</span> <b>${esc(e.user_name || "?")}</b>
              <span class="muted">· ${esc(e.workflow)} · ${fmtAgo(anyTs(e.created_at))} · now ${esc(e.status)}</span>
              <div style="margin-top:3px;color:var(--ink-2)">"${esc(e.feedback_text)}"</div>
            </div>`).join("")}
          </div>
        </details>
        <div class="muted mono" style="font-size:11px;margin-top:3px">#${r.id} · ${esc(r.synthesized_at || "")}${r.reviewed_by ? ` · reviewed by ${esc(r.reviewed_by)}` : ""}</div>
      </td>
      <td>${scopePill(r.scope)}${r.workflow ? `<div class="muted" style="font-size:11px">${esc(r.workflow)}</div>` : ""}</td>
      <td class="right">${(r.confidence ?? 0).toFixed(2)}</td>
      <td class="nowrap">${ev.length} item(s)<div class="muted" style="font-size:11px">${esc(people.slice(0, 3).join(", "))}</div></td>
      <td>${pillStatus(r.status)}</td>`;
    },
    filters: [
      { name: "status", type: "select", options: [
        { value: "pending", label: "Pending review" },
        { value: "deferred", label: "Parked (no rule)" },
        { value: "approved", label: "Approved" },
        { value: "rejected", label: "Rejected" },
        { value: "all", label: "All" }] },
      { name: "scope", type: "select", options: [
        { value: "", label: "All scopes" },
        { value: "global", label: "global" },
        { value: "workflow", label: "workflow" },
        { value: "engineering", label: "engineering" },
        { value: "candidate_specific", label: "candidate-specific" },
        { value: "insufficient_evidence", label: "needs evidence" }] },
      { name: "__q", type: "search", label: "Filter lessons…" },
    ],
    actions: [
      { key: "a", label: "Approve", kind: "ok", batch: "lessons.approve",
        url: (r) => `/admin/api/lessons/${r.id}/approve`, body: () => ({}),
        confirmLabel: (x) => typeof x === "number" ? `Approving ${x} lessons` : `Approving lesson #${x.id}` },
      { key: "r", label: "Reject", kind: "danger", batch: "lessons.reject",
        url: (r) => `/admin/api/lessons/${r.id}/reject`, body: () => ({}),
        confirmLabel: (x) => typeof x === "number" ? `Rejecting ${x} lessons` : `Rejecting lesson #${x.id}` },
    ],
    editor: {
      saveLabel: "approve",
      fields: (r) => [
        { name: "edited_text", label: "Lesson text (approves with this wording)", value: r.lesson_text, textarea: true },
        { name: "note", label: "Your note (optional) — “yes, but also consider…”; travels with the rule", value: "", textarea: true }],
      action: { label: "Approve edited", batch: "lessons.approve",
        url: (r) => `/admin/api/lessons/${r.id}/approve`,
        body: (r, v) => ({ edited_text: v.edited_text, note: v.note }),
        confirmLabel: (r) => `Approving lesson #${r.id} (edited)` },
    },
    emptyText: "No lessons here. Hit Synthesize to have Noto read the pending feedback.",
  });

  $("#ls-synth").onclick = async () => {
    try {
      const r = await api("/admin/api/lessons/synthesize", { method: "POST", body: {} });
      notify(r.message || "synthesis started", "ok", 6000);
      const poll = setInterval(async () => {
        const c = await api("/admin/api/inbox/counts").catch(() => null);
        if (c && !c.synth_running) { clearInterval(poll); q.refresh(); refreshCounts(); }
      }, 4000);
      setTimeout(() => clearInterval(poll), 300000);
    } catch (e) { notify(e.message, "bad", 6000); }
  };
  return q;
}

function inboxRules(host) {
  return Queue(host, {
    load: async (f) => {
      const qs = new URLSearchParams();
      if (f.status) qs.set("status", f.status);
      if (f.workflow) qs.set("workflow", f.workflow);
      const d = await api(`/admin/api/rules?${qs}`);
      return { rows: d.rules, facets: { workflow: d.workflows } };
    },
    rowKey: (r) => r.id,
    header: `<th class="primary">Rule</th><th>Workflow</th><th>Priority</th><th class="right">Support</th><th>Status</th><th>When</th>`,
    renderCells: (r) => `
      <td class="primary">
        <div>${esc(r.rule_text)}</div>
        <div class="muted mono" style="font-size:11px;margin-top:3px">#${r.id} · ${esc(r.source_type || "")}${r.reviewed_by ? ` · reviewed by ${esc(r.reviewed_by)}` : ""} · <a data-ev href="#" style="font-family:var(--font)">evidence</a></div>
      </td>
      <td><span class="pill dim">${esc(r.workflow)}</span></td>
      <td>${r.priority === "high" ? '<span class="pill warn">high</span>' : '<span class="pill dim">normal</span>'}</td>
      <td class="right">${r.support_count ?? 1}</td>
      <td>${pillStatus(r.status)}</td>
      <td class="muted nowrap">${fmtAgo(anyTs(r.recommended_at))}</td>`,
    filters: [
      { name: "status", type: "select", options: [
        { value: "pending", label: "Pending" }, { value: "approved", label: "Approved" },
        { value: "rejected", label: "Rejected" }, { value: "all", label: "All" }] },
      { name: "workflow", type: "select", options: [{ value: "", label: "All workflows" }] },
      { name: "__q", type: "search", label: "Filter rules…" },
    ],
    actions: [
      { key: "a", label: "Approve", kind: "ok", batch: "rules.approve",
        url: (r) => `/admin/api/rules/${r.id}/approve`, body: () => ({}),
        confirmLabel: (x) => typeof x === "number" ? `Approving ${x} rules` : `Approving rule #${x.id}` },
      { key: "r", label: "Reject", kind: "danger", batch: "rules.reject",
        url: (r) => `/admin/api/rules/${r.id}/reject`, body: () => ({}),
        confirmLabel: (x) => typeof x === "number" ? `Rejecting ${x} rules` : `Rejecting rule #${x.id}` },
    ],
    editor: {
      saveLabel: "approve",
      fields: (r) => [
        { name: "edited_text", label: "Rule text (approves with this wording)", value: r.rule_text, textarea: true },
        { name: "note", label: "Your note (optional) — appended with the rule", value: "", textarea: true }],
      action: { label: "Approve edited", batch: "rules.approve",
        url: (r) => `/admin/api/rules/${r.id}/approve`,
        body: (r, v) => ({ edited_text: v.edited_text, note: v.note }),
        confirmLabel: (r) => `Approving rule #${r.id} (edited)` },
    },
    decorateRow: (tr, r) => {
      const a = tr.querySelector("[data-ev]");
      if (a) a.onclick = async (e) => {
        e.preventDefault();
        try {
          const d = await api(`/admin/api/rules/${r.id}/evidence`);
          const body = h("<div></div>");
          body.appendChild(frag(`<h3>Rule</h3><p style="margin:0 0 6px">${esc(r.rule_text)}</p>
            <p class="muted" style="font-size:12px;margin:0">pattern <span class="mono">${esc(r.pattern_signature || "")}</span> · ${d.events.length} supporting edit(s)</p>`));
          for (const ev of d.events) {
            body.appendChild(frag(`<h3>Event #${ev.id} — ${esc(ev.source)} · ${esc(ev.user_name || "?")} (${esc(ev.authority || "standard")})</h3>
              <table class="kv">
                ${ev.candidate ? `<tr><th>Candidate</th><td>${esc(ev.candidate)}</td></tr>` : ""}
                ${ev.doc_url ? `<tr><th>Doc</th><td><a href="${esc(ev.doc_url)}" target="_blank" rel="noopener">open</a></td></tr>` : ""}
                <tr><th>When</th><td>${esc(fmtTs(anyTs(ev.captured_at)))} · ${esc(ev.change_type || "")} · conf ${(ev.confidence ?? 0).toFixed(2)}</td></tr>
                ${ev.instruction ? `<tr><th>Instruction</th><td>${esc(ev.instruction)}</td></tr>` : ""}
              </table>
              ${ev.diff ? `<div style="margin:2px 0 0"><div class="muted" style="font-size:11px;letter-spacing:.05em;text-transform:uppercase;margin:0 0 4px">What changed</div><pre class="blob diff" style="max-height:280px">${diffHtml(ev.diff)}</pre></div>` : ""}`));
          }
          openDrawer(`Evidence — rule #${r.id}`, body);
        } catch (err) { notify(err.message, "bad", 5000); }
      };
    },
    emptyText: "No rules match — the queue is clear.",
  });
}

function inboxTriage(host) {
  host.appendChild(h(`<p class="muted" style="margin:0 0 10px;font-size:12px">
    Raw feedback is <b>evidence</b> — it becomes a rule only through an
    approved lesson (the Lessons tab). Here you can inspect it, reject
    junk, or reclassify; approving/rejecting a lesson resolves its
    supporting items automatically.</p>`));
  return Queue(host, {
    load: async (f) => {
      const qs = new URLSearchParams();
      if (f.status) qs.set("status", f.status);
      if (f.workflow) qs.set("workflow", f.workflow);
      if (f.kind) qs.set("kind", f.kind);
      const d = await api(`/admin/api/feedback?${qs}`);
      return { rows: d.feedback, facets: { workflow: d.workflows } };
    },
    rowKey: (r) => r.id,
    header: `<th class="primary">Feedback</th><th>From</th><th>Kind</th><th>Lesson</th><th>Status</th><th>When</th>`,
    renderCells: (r) => {
      const lifecycle = r.lesson_id
        ? `<a data-lesson href="#" class="pill ${
            { pending: "warn", deferred: "dim", approved: "ok",
              rejected: "bad", superseded: "dim" }[r.lesson_status] || "dim"
          }" style="text-decoration:none" title="jump to lesson">→ #${r.lesson_id} ${esc(r.lesson_status)}</a>`
        : '<span class="muted" style="font-size:11.5px">not yet synthesized</span>';
      return `
      <td class="primary">
        <div>${esc(r.feedback_text)}</div>
        ${r.context_snippet ? `<div class="muted" style="font-size:11.5px;margin-top:3px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:700px">${esc(r.context_snippet)}</div>` : ""}
        <div class="muted mono" style="font-size:11px;margin-top:2px">#${r.id} · ${esc(r.source || "")} · ${esc(r.workflow)}</div>
      </td>
      <td class="nowrap"><b>${esc(!r.user_name ? "?" : r.user_name.startsWith("ou_") ? "·" + r.user_name.slice(-6) : r.user_name)}</b></td>
      <td>${pillKind(r.kind)}</td>
      <td class="nowrap">${lifecycle}</td>
      <td>${pillStatus(r.status)}${r.resolution_note ? `<div class="muted" style="font-size:11px">${esc(r.resolution_note)}</div>` : ""}</td>
      <td class="muted nowrap">${fmtAgo(anyTs(r.created_at))}</td>`;
    },
    filters: [
      { name: "status", type: "select", options: [
        { value: "unresolved", label: "Unresolved" }, { value: "accepted", label: "Accepted" },
        { value: "rejected", label: "Rejected" }, { value: "all", label: "All" }] },
      { name: "kind", type: "select", options: [
        { value: "", label: "All kinds" }, { value: "rule", label: "rule" },
        { value: "engineering", label: "engineering" }, { value: "both", label: "both" },
        { value: "unsure", label: "unsure" }] },
      { name: "workflow", type: "select", options: [{ value: "", label: "All workflows" }] },
      { name: "__q", type: "search", label: "Filter feedback…" },
    ],
    actions: [
      { key: "r", label: "Reject", kind: "danger", batch: "feedback.reject",
        url: (r) => `/admin/api/feedback/${r.id}/reject`, body: () => ({ note: "rejected as junk from panel" }),
        confirmLabel: (x) => typeof x === "number" ? `Rejecting ${x} items` : `Rejecting #${x.id}` },
    ],
    decorateRow: (tr, r, ctx) => {
      /* lifecycle link → jump to the Lessons tab */
      const jump = tr.querySelector("[data-lesson]");
      if (jump) jump.onclick = (e) => {
        e.preventDefault();
        inboxOpenTab && inboxOpenTab("lessons");
      };
      /* quick reclassify: click the kind pill to cycle rule→eng→both→unsure */
      const cell = tr.children[3];
      cell.style.cursor = "pointer";
      cell.title = "Click to reclassify";
      cell.onclick = async () => {
        const order = ["rule", "engineering", "both", "unsure"];
        const next = order[(order.indexOf(r.kind) + 1) % order.length];
        try {
          await api(`/admin/api/feedback/${r.id}/reclassify`, { method: "POST", body: { kind: next } });
          r.kind = next; ctx.refresh();
        } catch (e) { notify(e.message, "bad"); }
      };
    },
    emptyText: "No feedback here — inbox zero.",
  });
}

function inboxNuggets(host) {
  host.appendChild(h(`<p class="muted" style="margin:0 0 8px;font-size:12px">
    Approving adds the nugget to the corpus as <b>one dated data point</b> —
    it's weighed with the documents at answer time, never as standalone
    policy. You can't damage the corpus by approving; judge the micro-lesson
    on its own merits and dismiss junk.</p>`));
  const embedNote = h('<div class="muted" style="margin:0 0 10px;font-size:12px"></div>');
  host.appendChild(embedNote);
  let queueRef = null;
  let lastUnchecked = state.counts.nuggets_unchecked ?? 0;
  const paintEmbed = () => {
    const n = state.counts.nuggets_unembedded ?? 0;
    const u = state.counts.nuggets_unchecked ?? 0;
    const bits = [];
    if (n) bits.push(`embedding ${n} approved nugget(s)`);
    if (u) bits.push(`pre-checking ${u} pending nugget(s) against the corpus`);
    embedNote.innerHTML = bits.length
      ? `${state.counts.embed_running ? '<span class="spin" style="width:11px;height:11px;vertical-align:-2px"></span> ' : ""}${bits.join(" · ")}…`
      : "";
    if (u < lastUnchecked) queueRef?.refresh();  // verdicts arrived — repaint
    lastUnchecked = u;
  };
  paintEmbed();
  const embedTimer = setInterval(() => paintEmbed(), 5000);

  const q = Queue(host, {
    load: async (f) => {
      const qs = new URLSearchParams();
      if (f.status) qs.set("status", f.status);
      if (f.chat) qs.set("chat", f.chat);
      const d = await api(`/admin/api/nuggets?${qs}`);
      return { rows: d.nuggets, facets: { chat: d.chats } };
    },
    rowKey: (r) => r.id,
    header: `<th class="primary">Q &amp; A</th><th>Chat</th><th>From</th><th>Status</th><th>When</th>`,
    renderCells: (r) => {
      const dur = r.durability === "ephemeral"
        ? '<span class="pill warn">time-sensitive</span>'
        : r.durability === "mixed"
        ? '<span class="pill warn">mixed durability</span>'
        : r.durability === "durable" ? '<span class="pill ok">durable</span>' : "";
      let contribs = [];
      try { contribs = JSON.parse(r.contributors || "[]"); } catch {}
      const answerer = r.answerer_name || contribs[0] || "";
      const others = contribs.filter(c => c !== answerer);
      return `
      <td class="primary">
        <div><b>Q:</b> ${esc(r.question)}</div>
        <div class="dim" style="margin-top:2px"><b>A:</b> ${esc(r.answer)}</div>
        ${r.durable_reframe ? `<div style="font-size:12px;margin-top:3px;color:var(--ok)"><b>Durable lesson:</b> ${esc(r.durable_reframe)}</div>` : ""}
        ${r.reviewed_note && r.reviewed_note !== "approved" && r.status !== "pending" ? `<div style="font-size:11.5px;margin-top:3px;color:var(--warn)"><b>Operator note:</b> ${esc(r.reviewed_note)}</div>` : ""}
        ${r.context_note ? `<div class="muted" style="font-size:11.5px;margin-top:3px"><b>Corpus cross-check:</b> ${esc(r.context_note)}</div>` : ""}
        <div class="muted mono" style="font-size:11px;margin-top:3px">#${r.id} · confidence ${(r.confidence ?? 0).toFixed(2)}${r.conflict_with ? ` · <span style="color:var(--warn)">conflicts with #${r.conflict_with}</span>` : ""}${r.context_note ? "" : ` · <a data-ctx href="#" style="font-family:var(--font)">check against corpus</a>`}</div>
      </td>
      <td class="muted nowrap" style="max-width:140px;overflow:hidden;text-overflow:ellipsis">${esc(r.chat_name || "?")}</td>
      <td style="max-width:170px"><b>${esc(answerer || "(name not synced)")}</b>
        ${others.length ? `<div class="muted" style="font-size:11px">with ${esc(others.join(", "))}</div>` : ""}
        <div class="muted" style="font-size:11px">${esc(r.authority || "")}</div></td>
      <td>${pillStatus(r.status)}${dur ? `<div style="margin-top:3px">${dur}</div>` : ""}${r.status === "active" && !r.embedded_at ? '<div class="muted" style="font-size:11px">embedding…</div>' : ""}</td>
      <td class="muted nowrap">${fmtAgo(anyTs(r.created_at))}</td>`;
    },
    decorateRow: (tr, r, ctx) => {
      const a = tr.querySelector("[data-ctx]");
      if (a) a.onclick = async (e) => {
        e.preventDefault();
        a.replaceWith(h('<span class="muted"><span class="spin" style="width:10px;height:10px;vertical-align:-1px"></span> checking corpus…</span>'));
        try {
          const res = await api(`/admin/api/nuggets/${r.id}/contextualize`, { method: "POST", body: {} });
          notify(`#${r.id}: ${res.durability || "checked"}`, "ok");
          ctx.refresh();
        } catch (err) { notify(err.message, "bad", 6000); ctx.refresh(); }
      };
    },
    filters: [
      { name: "status", type: "select", options: [
        { value: "pending", label: "Pending" }, { value: "active", label: "Active" },
        { value: "superseded", label: "Dismissed" }, { value: "all", label: "All" }] },
      { name: "chat", type: "select", options: [{ value: "", label: "All chats" }] },
      { name: "__q", type: "search", label: "Filter nuggets…" },
    ],
    actions: [
      { key: "a", label: "Approve", kind: "ok", batch: "nuggets.approve",
        url: (r) => `/admin/api/nuggets/${r.id}/approve`, body: () => ({}),
        confirmLabel: (x) => typeof x === "number" ? `Approving ${x} nuggets` : `Approving nugget #${x.id}` },
      { key: "r", label: "Dismiss", kind: "danger", batch: "nuggets.dismiss",
        url: (r) => `/admin/api/nuggets/${r.id}/dismiss`, body: () => ({}),
        confirmLabel: (x) => typeof x === "number" ? `Dismissing ${x} nuggets` : `Dismissing nugget #${x.id}` },
    ],
    editor: {
      saveLabel: "approve",
      fields: (r) => [
        { name: "edited_question", label: "Question", value: r.question, textarea: true },
        { name: "edited_answer", label: "Answer", value: r.answer, textarea: true },
        { name: "note", label: "Your note (optional) — “yes, but also consider…”; embedded with the nugget", value: "", textarea: true }],
      action: { label: "Approve edited", batch: "nuggets.approve",
        url: (r) => `/admin/api/nuggets/${r.id}/approve`,
        body: (r, v) => v,
        confirmLabel: (r) => `Approving nugget #${r.id} (edited)` },
    },
    emptyText: "No nuggets match — the knowledge base is curated.",
  });
  queueRef = q;
  return { ...q, cleanup: () => { clearInterval(embedTimer); q.cleanup(); } };
}

/* ══ DRAWER + DIFF HELPERS ══════════════════════════════════════════ */

let drawerEl = null;
function openDrawer(title, bodyEl) {
  closeDrawer();
  drawerEl = h(`<div><div class="drawer-overlay"></div>
    <div class="drawer">
      <div class="drawer-head"><h2>${esc(title)}</h2><button class="x">✕</button></div>
      <div class="drawer-body"></div>
    </div></div>`);
  $(".drawer-body", drawerEl).appendChild(bodyEl);
  $(".x", drawerEl).onclick = closeDrawer;
  $(".drawer-overlay", drawerEl).onclick = closeDrawer;
  document.body.appendChild(drawerEl);
}
function closeDrawer() { drawerEl?.remove(); drawerEl = null; }

/* unified-diff → colored lines (added green, removed red, hunks muted) */
function diffHtml(diff) {
  return diff.split("\n").map(l => {
    const cls = l.startsWith("+") ? "d-add"
      : l.startsWith("-") ? "d-del"
      : l.startsWith("@@") ? "d-hunk" : "";
    return `<span class="${cls}">${esc(l)}</span>`;
  }).join("\n");
}

/* ══ HEALTH WORKSPACE (Phase 6) ══════════════════════════════════════ */

const fmtBytes = (n) => {
  if (n == null) return "—";
  if (n < 1e6) return `${(n / 1e3).toFixed(0)} KB`;
  if (n < 1e9) return `${(n / 1e6).toFixed(1)} MB`;
  return `${(n / 1e9).toFixed(2)} GB`;
};
const ageClass = (mtime, freshH = 26, warnD = 7) => {
  if (!mtime) return "bad";
  const h = (Date.now() / 1000 - mtime) / 3600;
  return h < freshH ? "ok" : h < warnD * 24 ? "warn" : "bad";
};

function renderHealth(el) {
  el.appendChild(h(`<div class="page-head">
    <h1>Health</h1><span class="sub">bot · auth · corpus · jobs</span>
    <div class="actions" id="hl-ops"></div>
  </div>`));
  const root = h('<div id="hl-root"><div class="empty-state"><span class="spin"></span></div></div>');
  el.appendChild(root);

  let timer = null;
  async function load() {
    let d;
    try { d = await api("/admin/api/health"); }
    catch (e) { notify(e.message, "bad", 5000); return; }
    paint(d);
  }

  function oauthTile(label, o) {
    if (!o || o.error) return { cls: "bad", val: "error", sub: o?.error || "no data" };
    if (!o.authorized) return { cls: "bad", val: "not authorized", sub: "" };
    if (!o.access_token_valid) return { cls: "warn", val: "expired", sub: "keepalive may have missed" };
    const rd = o.refresh_expires_in_days ?? 0;
    return { cls: rd < 2 ? "warn" : "ok", val: `${rd.toFixed(1)}d`,
             sub: `refresh window · access ${(o.access_expires_in_s / 3600).toFixed(1)}h` };
  }

  function paint(d) {
    const bot = d.bot || {};
    const up = bot.start_time ? fmtAgo(bot.start_time).replace(" ago", "") : "?";
    const opT = oauthTile("operator", d.oauth?.operator);
    const noT = oauthTile("noah", d.oauth?.noah);
    const resyncM = d.resync?.log_mtime;
    root.replaceChildren(h(`<div class="fade-in">
      ${d.oauth_alert ? `<div class="panel" style="border-color:var(--bad)"><div class="panel-body" style="color:var(--bad)">⚠ OAuth alert: <span class="mono">${esc(d.oauth_alert)}</span></div></div>` : ""}
      <div class="tiles">
        <div class="tile ${bot.pid ? "ok" : "bad"}"><div class="lbl">Bot</div>
          <div class="val">${bot.pid ? "up " + esc(up) : "down"}</div>
          <div class="sub">pid ${esc(bot.pid || "—")} · ${esc(String(bot.messages_handled ?? "?"))} msgs · ${esc(bot.branch)}@${esc(bot.commit)}</div></div>
        <div class="tile ${opT.cls}"><div class="lbl">OAuth · operator</div>
          <div class="val">${esc(opT.val)}</div><div class="sub">${esc(opT.sub)}</div></div>
        <div class="tile ${noT.cls}"><div class="lbl">OAuth · noah</div>
          <div class="val">${esc(noT.val)}</div><div class="sub">${esc(noT.sub)}</div></div>
        <div class="tile ${d.funnel?.ok ? "ok" : "bad"}"><div class="lbl">Funnel</div>
          <div class="val">${d.funnel?.ok ? "serving" : "down"}</div>
          <div class="sub">funnel → 127.0.0.1</div></div>
        <div class="tile ${d.resync?.running ? "ok" : ageClass(resyncM)}"><div class="lbl">Nightly resync</div>
          <div class="val">${d.resync?.running ? "running" : resyncM ? fmtAgo(resyncM) : "never"}</div>
          <div class="sub">${esc((d.resync?.last_done || "").slice(0, 40) || "no completion line found")}</div></div>
        <div class="tile"><div class="lbl">Keepalive</div>
          <div class="val">${d.keepalive_log_mtime ? fmtAgo(d.keepalive_log_mtime) : "—"}</div>
          <div class="sub">oauth keepalive log</div></div>
      </div>

      <div class="panel"><div class="panel-head"><h2>Index freshness</h2></div>
        <div class="panel-body flush"><table class="grid"><thead><tr>
          <th>Index</th><th>Size</th><th>Updated</th><th></th>
        </tr></thead><tbody>
          ${(d.indexes || []).map(ix => `<tr>
            <td class="mono">${esc(ix.name)}</td>
            <td class="muted">${fmtBytes(ix.size)}</td>
            <td class="muted" title="${esc(fmtTs(ix.mtime))}">${fmtAgo(ix.mtime)}</td>
            <td><span class="pill ${ageClass(ix.mtime)}">${{ ok: "fresh", warn: "aging", bad: "stale" }[ageClass(ix.mtime)]}</span></td>
          </tr>`).join("")}
        </tbody></table></div></div>

      <div class="panel"><div class="panel-head"><h2>launchd jobs</h2></div>
        <div class="panel-body flush"><table class="grid"><thead><tr>
          <th>Label</th><th>PID</th><th>Last exit</th>
        </tr></thead><tbody>
          ${(d.launchd || []).map(j => `<tr>
            <td class="mono">${esc(j.label)}</td>
            <td>${j.pid ? `<span class="pill ok">${esc(j.pid)}</span>` : '<span class="muted">idle</span>'}</td>
            <td>${j.last_exit === "0" ? '<span class="pill ok">0</span>' : `<span class="pill bad">${esc(j.last_exit)}</span>`}</td>
          </tr>`).join("") || '<tr><td colspan="3" class="muted">none found</td></tr>'}
        </tbody></table></div></div>

      <div class="panel"><div class="panel-head"><h2>Resync log · tail</h2></div>
        <div class="panel-body"><pre class="blob">${esc((d.resync?.tail || []).join("\n") || "(no log)")}</pre></div></div>

      <div class="panel"><div class="panel-head"><h2>Funnel status</h2></div>
        <div class="panel-body"><pre class="blob">${esc(d.funnel?.raw || "(unavailable)")}</pre></div></div>
    </div>`));

    /* ops buttons — super_admin only, and only when the server allows */
    const ops = $("#hl-ops");
    ops.replaceChildren();
    if (state.me.role === "super_admin") {
      const mk = (label, kind, fn) => {
        const b = h(`<button class="btn sm ${kind}">${esc(label)}</button>`);
        b.onclick = fn; ops.appendChild(b); return b;
      };
      const disabled = !d.ops_enabled;
      const note = disabled ? " (disabled in dev sandbox)" : "";
      const b1 = mk("Run resync", "", async () => {
        try { const r = await api("/admin/api/ops/resync", { method: "POST", body: {} });
              notify(r.message || "resync started", "ok", 5000); setTimeout(load, 1500); }
        catch (e) { notify(e.message, "bad", 6000); }
      });
      const b2 = mk("Cycle tunnel", "", async () => {
        try { await api("/admin/api/ops/tunnel", { method: "POST", body: {} });
              notify("funnel re-asserted", "ok"); setTimeout(load, 1000); }
        catch (e) { notify(e.message, "bad", 6000); }
      });
      const b3 = mk("Restart bot…", "danger", () => confirmRestart(load));
      if (disabled) [b1, b2, b3].forEach(b => { b.disabled = true; b.title = "ops run only from the production process"; });
      ops.appendChild(h(`<span class="muted" style="font-size:11px;align-self:center">${esc(note)}</span>`));
    }
  }

  function confirmRestart(reload) {
    const ov = h(`<div class="help-overlay"><div class="help-card">
      <h2 style="color:var(--bad)">Restart the bot</h2>
      <p class="dim">This kills the live process — all in-flight webhook work and
      any streaming card mid-render are dropped. Users mid-conversation
      will see their request die. Type <b>RESTART</b> to confirm.</p>
      <div class="row" style="margin-top:12px">
        <input class="input mono" id="rs-confirm" placeholder="RESTART">
        <button class="btn danger" id="rs-go">Restart</button>
        <button class="btn ghost" id="rs-cancel">Cancel</button>
      </div></div></div>`);
    document.body.appendChild(ov);
    $("#rs-cancel", ov).onclick = () => ov.remove();
    ov.onclick = (e) => { if (e.target === ov) ov.remove(); };
    $("#rs-go", ov).onclick = async () => {
      try {
        const r = await api("/admin/api/ops/restart-bot", { method: "POST",
          body: { confirm: $("#rs-confirm", ov).value.trim() } });
        ov.remove();
        notify(r.message || "restart dispatched", "ok", 8000);
        setTimeout(reload, 4000);
      } catch (e) { notify(e.message, "bad", 6000); }
    };
    $("#rs-confirm", ov).focus();
  }

  load();
  timer = setInterval(load, 30000);
  state.viewKeys = (e) => false;
  /* stop polling when the user navigates away */
  const stop = () => { clearInterval(timer); window.removeEventListener("hashchange", stop); };
  window.addEventListener("hashchange", stop);
}

/* ── bar-chart rows helper (Use cases) ── */
function barRows(rows) {
  const mx = Math.max(...rows.map(r => r.v), 1);
  return rows.map(r => `<div class="barrow">
    <div class="lbl" title="${esc(r.label)}">${esc(r.label)}</div>
    <div class="track"><div class="fill" style="width:${Math.max(2, (r.v / mx) * 100)}%"></div></div>
    <div class="num">${esc(r.num)}</div>
  </div>`).join("");
}

/* ══ USE CASES WORKSPACE ═════════════════════════════════════════════ */

function renderUseCases(el) {
  el.appendChild(h(`<div class="page-head">
    <h1>Use cases</h1>
    <span class="sub">what the team asks Noto to do — the dashes are coaching opportunities</span>
  </div>`));
  const root = h('<div><div class="empty-state"><span class="spin"></span></div></div>');
  el.appendChild(root);

  (async () => {
    let d;
    try { d = await api("/admin/api/usage?days=7"); }
    catch (e) { notify(e.message, "bad", 5000); return; }
    if (!(d.actions || []).length) {
      root.replaceChildren(h(`<div class="panel"><div class="empty-state">
        <div class="big">◌</div>
        <div><b>No use-case data yet.</b></div>
        <div style="margin-top:6px;max-width:520px;margin-left:auto;margin-right:auto">
          Every message gets classified by the skill Noto used for it —
          research questions, doc edits, commands and more — starting
          from the next bot restart. Within a week or two this page shows
          the team's most common use cases and who's using (or missing)
          each one.</div>
      </div></div>`));
      return;
    }
    const nameOf = {};
    (d.users || []).forEach(u => nameOf[u.open_id] = u.display_name || "·" + u.open_id.slice(-6));
    const topActs = d.actions.slice(0, 8).map(a => a.action_type);
    const byUser = {};
    (d.actions_by_user || []).forEach(r => {
      (byUser[r.open_id] = byUser[r.open_id] || {})[r.action_type] = r.n;
    });
    const matrix = Object.entries(byUser)
      .sort((a, b) => Object.values(b[1]).reduce((x, y) => x + y, 0)
                    - Object.values(a[1]).reduce((x, y) => x + y, 0));
    root.replaceChildren(h(`<div class="fade-in">
      <div class="panel"><div class="panel-head"><h2>Distribution · 30d</h2></div>
        <div class="panel-body">${barRows(d.actions.map(a => ({
          label: a.action_type.replace(/_/g, " "), v: a.messages,
          num: `${a.messages} · ${a.users} user(s)` })))}</div></div>
      <div class="panel"><div class="panel-head"><h2>Coverage by user · 30d</h2>
        <span class="muted" style="font-size:11.5px;margin-left:auto">— means never used it: your "did you know Noto can…" list</span></div>
        <div class="panel-body flush" style="overflow-x:auto"><table class="grid"><thead><tr>
          <th class="primary">User</th>${topActs.map(t => `<th class="right">${esc(t.replace(/_/g, " "))}</th>`).join("")}
        </tr></thead><tbody>
        ${matrix.map(([oid, acts]) => `<tr>
          <td class="primary"><b>${esc(nameOf[oid] || "·" + oid.slice(-6))}</b></td>
          ${topActs.map(t => acts[t]
            ? `<td class="right"><b>${acts[t]}</b></td>`
            : `<td class="right" style="color:var(--ink-3)">—</td>`).join("")}
        </tr>`).join("")}
        </tbody></table></div></div>
    </div>`));
  })();
  state.viewKeys = (e) => false;
}

/* ── boot ─────────────────────────────────────────────────────────── */
async function boot() {
  try {
    const me = await api("/admin/api/me");
    state.me = me;
    await refreshCounts().catch(() => {});
    renderShell();
  } catch {
    /* 401 path already rendered login */
  }
}
boot();
