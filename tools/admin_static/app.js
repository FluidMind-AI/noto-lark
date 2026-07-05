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
  pipeline: '<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 4h16v4H4z"/><path d="M6 12h12v4H6z"/><path d="M9 20h6"/></svg>',
  candidates: '<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75"/></svg>',
  recruiters: '<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 3v18h18"/><path d="m7 14 4-4 3 3 5-6"/></svg>',
  health: '<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 12h-4l-3 9L9 3l-3 9H2"/></svg>',
  usecases: '<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/></svg>',
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
  pipeline: {
    title: "Pipeline", icon: "pipeline", key: "p",
    badge: () => state.counts.open_polls ?? 0,
    render: (el) => renderPipeline(el),
  },
  candidates: {
    title: "Candidates", icon: "candidates", key: "c",
    render: (el) => renderCandidates(el),
  },
  recruiters: {
    title: "Recruiters", icon: "recruiters", key: "r",
    render: (el) => renderRecruiters(el),
  },
  usecases: {
    title: "Use cases", icon: "usecases", key: "u",
    render: (el) => renderUseCases(el),
  },
  health: {
    title: "Health", icon: "health", key: "h",
    render: (el) => renderHealth(el),
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
      if (r.ok && d.ok) { boot(); }
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
      <input placeholder="Jump to a workspace or search candidates…">
      <div class="results"></div>
    </div></div>`);
  document.body.appendChild(paletteEl);
  const input = $("input", paletteEl);
  const results = $(".results", paletteEl);
  let cursor = 0, shown = items, candHits = [];

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
    if (candHits.length) {
      blocks.push(h(`<div class="group">Candidates</div>`));
      candHits.forEach((c, j) => {
        const i = shown.length + j;
        const el = h(`<div class="item ${i === cursor ? "cursor" : ""}">
          <span>${esc(c.name)}</span>
          <span class="k muted" style="font-size:11px">${esc(c.practice || "")}</span></div>`);
        el.onclick = () => { close(); c.run(); };
        blocks.push(el);
      });
    }
    results.replaceChildren(...blocks);
    if (!blocks.length) results.replaceChildren(h(`<div class="empty-state">No matches</div>`));
  }
  function close() { paletteEl.remove(); paletteEl = null; }
  const allShown = () => shown.concat(candHits);

  let candDeb;
  input.oninput = () => {
    const q = input.value.trim().toLowerCase();
    shown = items.filter(it => it.label.toLowerCase().includes(q));
    cursor = 0; paint();
    clearTimeout(candDeb);
    if (q.length >= 2) {
      candDeb = setTimeout(async () => {
        try {
          const d = await api(`/admin/api/candidates?q=${encodeURIComponent(q)}&limit=8`);
          candHits = d.rows.map(r => ({
            name: r.name, practice: r.practice,
            run: () => { location.hash = `#/candidates/${encodeURIComponent(r.key)}`; },
          }));
        } catch { candHits = []; }
        paint();
      }, 200);
    } else { candHits = []; paint(); }
  };
  input.onkeydown = (e) => {
    if (e.key === "Escape") close();
    else if (e.key === "ArrowDown") { cursor = Math.min(cursor + 1, allShown().length - 1); paint(); }
    else if (e.key === "ArrowUp") { cursor = Math.max(cursor - 1, 0); paint(); }
    else if (e.key === "Enter" && allShown()[cursor]) { close(); allShown()[cursor].run(); }
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
    ["⌘K", "command palette"], ["g i", "Inbox"], ["g p", "Pipeline"],
    ["g c", "Candidates"], ["g r", "Recruiters"], ["g u", "Use cases"], ["g h", "Health"],
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
            body.appendChild(frag(`<h3>Event #${ev.id} — ${esc(ev.source)} · ${esc(ev.recruiter_name || "?")} (${esc(ev.authority || "standard")})</h3>
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

/* ══ PIPELINE WORKSPACE (Phase 4, read-only) ═════════════════════════ */

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

const verdictPill = (v) => ({
  status_change: '<span class="pill ok">status change</span>',
  calendar_event: '<span class="pill acc">calendar</span>',
  both: '<span class="pill ok">both</span>',
  ambiguous: '<span class="pill dim">ambiguous</span>',
  noise: '<span class="pill dim">noise</span>',
  stale_calendar: '<span class="pill dim">stale calendar</span>',
  superseded: '<span class="pill dim">superseded</span>',
  pipeline_event: '<span class="pill ok">pipeline event</span>',
}[v] || (v ? `<span class="pill dim">${esc(v)}</span>` : '<span class="pill warn">unextracted</span>'));

/* unified-diff → colored lines (added green, removed red, hunks muted) */
function diffHtml(diff) {
  return diff.split("\n").map(l => {
    const cls = l.startsWith("+") ? "d-add"
      : l.startsWith("-") ? "d-del"
      : l.startsWith("@@") ? "d-hunk" : "";
    return `<span class="${cls}">${esc(l)}</span>`;
  }).join("\n");
}

const pollPill = (s) => ({
  open: '<span class="pill warn">open</span>',
  approved: '<span class="pill ok">approved</span>',
  rejected: '<span class="pill bad">rejected</span>',
  edited: '<span class="pill acc">edited</span>',
  expired: '<span class="pill dim">expired</span>',
  error: '<span class="pill bad">error</span>',
}[s] || (s ? `<span class="pill dim">${esc(s)}</span>` : ""));

function renderPipeline(el) {
  el.appendChild(h(`<div class="page-head">
    <h1>Pipeline</h1>
    <span class="sub">every inbound email → verdict → poll → what got written</span>
  </div>`));

  /* ── open polls: actionable, same contract as the Lark card ── */
  const pollsPanel = h('<div></div>');
  el.appendChild(pollsPanel);
  async function loadPolls() {
    let d;
    try { d = await api("/admin/api/pipeline/polls"); }
    catch { return; }
    if (!d.polls?.length) { pollsPanel.replaceChildren(); return; }
    pollsPanel.replaceChildren(h(`<div class="panel" style="border-color:var(--warn-bg)">
      <div class="panel-head"><h2>Open polls · ${d.polls.length} awaiting a decision</h2></div>
      <div class="panel-body flush"><table class="grid"><thead><tr>
        <th class="primary">Proposed change</th><th>Type</th><th>Age</th><th>Expires</th><th></th>
      </tr></thead><tbody>
        ${d.polls.map(pl => {
          const firmScope = pl.poll_type === "status_change" && (pl.scope === "firm"
            || (!pl.scope && ["rejected", "on_hold"].includes(pl.proposed_status)));
          return `<tr data-poll="${pl.id}" data-msg="${esc(pl.source_message_id || "")}" style="cursor:pointer">
          <td class="primary"><b>${esc(pl.candidate_name || "?")}</b>
            ${pl.proposed_status ? ` → <span class="pill ${firmScope ? "dim" : "acc"}">${esc(pl.proposed_status)}</span>` : ""}
            ${pl.firm ? ` at <b>${esc(pl.firm)}</b>` : ""}
            ${firmScope ? ' <span class="pill warn" title="approving writes a dated note under this firm in the summary doc — overall CRM status unchanged">firm note only</span>' : ""}
            ${pl.note ? `<div class="muted" style="font-size:11.5px;margin-top:2px">${esc((pl.note || "").slice(0, 140))}</div>` : ""}
            <div class="muted mono" style="font-size:11px;margin-top:2px">poll #${pl.id} · click for the source email &amp; reasoning</div></td>
          <td><span class="pill dim">${esc(pl.poll_type)}</span></td>
          <td class="muted nowrap">${fmtAgo(anyTs(pl.created_at))}</td>
          <td class="muted nowrap">${pl.expires_in_s ? (pl.expires_in_s / 3600).toFixed(1) + "h" : "—"}</td>
          <td><div class="rowactions" style="opacity:1">
            <button class="btn sm ok" data-pa="approve">Approve</button>
            <button class="btn sm danger" data-pa="reject">Reject</button>
          </div></td>
        </tr>`;}).join("")}
      </tbody></table></div></div>`));
    /* row click → the same evidence drawer as the email table below:
       source email body, LLM extraction + reasoning, poll summary */
    pollsPanel.querySelectorAll("tr[data-poll]").forEach(tr => {
      tr.addEventListener("click", (e) => {
        if (e.target.closest("button")) return;
        if (tr.dataset.msg) openEmail({ message_id: tr.dataset.msg });
        else notify("No source email recorded for this poll", "bad");
      });
    });
    pollsPanel.querySelectorAll("[data-pa]").forEach(btn => btn.onclick = () => {
      const tr = btn.closest("tr");
      const pid = tr.dataset.poll;
      const act = btn.dataset.pa;
      tr.style.display = "none";
      queueAction({
        label: `${act === "approve" ? "Approving" : "Rejecting"} poll #${pid}`,
        url: `/admin/api/pipeline/poll/${pid}/${act}`,
        body: {},
        onCommit: (res) => {
          const ar = res.apply_result;
          if (ar && ar.ok === false) notify(`Poll #${pid}: applied with issues — see drawer`, "bad", 6000);
          loadPolls(); load(); refreshCounts();
        },
        onUndo: () => { tr.style.display = ""; },
      });
    });
  }
  loadPolls();

  const chips = h('<div class="chips"></div>');
  const fbar = h(`<div class="row" style="margin:0 0 12px;flex-wrap:wrap">
    <input class="input" id="pf-cand" placeholder="Candidate…" style="max-width:190px">
    <input class="input" id="pf-firm" placeholder="Firm…" style="max-width:190px">
    <select class="input" id="pf-resolver" style="width:auto"><option value="">Anyone</option></select>
    <label class="row" style="gap:5px;color:var(--ink-2);font-size:12.5px">
      <input type="checkbox" id="pf-haspoll"> polls only</label>
    <span class="muted" id="pf-count" style="margin-left:auto"></span>
  </div>`);
  const panel = h(`<div class="panel"><div class="panel-body flush">
    <table class="grid"><thead><tr>
      <th>When</th><th>Email</th><th>Verdict</th><th>Candidate</th><th>Firm</th>
      <th>Poll</th><th>Applied</th>
    </tr></thead><tbody></tbody></table>
    <div style="padding:10px 14px"><button class="btn sm ghost" id="pf-more" style="display:none">Load more</button></div>
  </div></div>`);
  el.append(chips, fbar, panel);
  const tbody = $("tbody", panel);

  let rows = [], cursor = 0, verdict = "", nextBefore = null, loading = false;

  async function load(append = false) {
    if (loading) return; loading = true;
    const qs = new URLSearchParams();
    if (verdict) qs.set("verdict", verdict);
    const cand = $("#pf-cand").value.trim(); if (cand) qs.set("candidate", cand);
    const firm = $("#pf-firm").value.trim(); if (firm) qs.set("firm", firm);
    const res = $("#pf-resolver").value; if (res) qs.set("resolved_by", res);
    if ($("#pf-haspoll").checked) qs.set("has_poll", "1");
    if (append && nextBefore) qs.set("before", nextBefore);
    try {
      const d = await api(`/admin/api/pipeline?${qs}`);
      rows = append ? rows.concat(d.rows) : d.rows;
      nextBefore = d.next_before;
      paintChips(d.verdict_counts);
      const sel = $("#pf-resolver");
      if (sel.options.length <= 1 && d.facets.resolved_by?.length) {
        d.facets.resolved_by.forEach(n => sel.appendChild(h(`<option>${esc(n)}</option>`)));
      }
      if (!append) cursor = 0;
      paint();
    } catch (e) { notify(e.message, "bad", 5000); }
    loading = false;
  }

  function paintChips(counts) {
    const entries = Object.entries(counts || {}).sort((a, b) => b[1] - a[1]);
    chips.replaceChildren(
      h(`<button class="chipstat ${verdict === "" ? "active" : ""}"><span>all</span><b>${entries.reduce((s, [, n]) => s + n, 0)}</b></button>`),
      ...entries.map(([v, n]) =>
        h(`<button class="chipstat ${verdict === v ? "active" : ""}"><span>${esc(v)}</span><b>${n}</b></button>`)));
    [...chips.children].forEach((c, i) => c.onclick = () => {
      verdict = i === 0 ? "" : entries[i - 1][0];
      load();
    });
  }

  function paint() {
    $("#pf-count").textContent = `${rows.length} loaded`;
    $("#pf-more").style.display = nextBefore ? "" : "none";
    tbody.replaceChildren(...rows.map((r, i) => {
      const applied = r.new_status
        ? `<span class="pill ok">${esc(r.old_status || "?")} → ${esc(r.new_status)}</span>
           <div class="muted" style="font-size:11px">${r.bitable_record_id ? "bitable ✓" : "bitable —"} · ${r.summary_doc_id ? "doc ✓" : "doc —"}</div>`
        : r.poll_status === "approved" ? '<span class="muted">—</span>' : "";
      const tr = h(`<tr class="${i === cursor ? "cursor" : ""}" style="cursor:pointer">
        <td class="muted nowrap" title="${esc(fmtTs(r.internal_date_ms / 1000))}">${fmtAgo(r.internal_date_ms / 1000)}</td>
        <td style="max-width:300px">
          <div style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap"><b>${esc(r.subject
            || (r.candidate_name ? `${r.candidate_name}${r.firm ? " · " + r.firm : ""}` : "")
            || (r.body_head || "").replace(/\s+/g, " ").trim().slice(0, 90)
            || "(empty email)")}</b></div>
          <div class="muted" style="font-size:11.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(r.from_name || r.from_email || (r.subject ? "?" : "headers unavailable — needs mail headers scope"))}</div>
        </td>
        <td>${verdictPill(r.extraction_verdict)}</td>
        <td class="nowrap">${esc(r.candidate_name || "")}</td>
        <td class="nowrap" style="max-width:150px;overflow:hidden;text-overflow:ellipsis">${esc(r.firm || "")}</td>
        <td>${pollPill(r.poll_status)}${r.resolved_by_name ? `<div class="muted" style="font-size:11px">${esc(r.resolved_by_name)} · ${fmtAgo(anyTs(r.resolved_at))}</div>` : ""}</td>
        <td>${applied}</td>
      </tr>`);
      tr.onclick = () => { cursor = i; paint(); openEmail(r); };
      return tr;
    }));
    if (!rows.length)
      tbody.replaceChildren(h(`<tr><td colspan="7"><div class="empty-state"><div class="big">◦</div>No emails match these filters.</div></td></tr>`));
  }

  async function openEmail(r) {
    try {
      const d = await api(`/admin/api/pipeline/email?id=${encodeURIComponent(r.message_id)}`);
      const e = d.email;
      const body = h('<div></div>');
      body.appendChild(frag(`<h3>Email</h3>
        <table class="kv">
          <tr><th>From</th><td>${esc(e.from_name || "")} &lt;${esc(e.from_email || "?")}&gt;</td></tr>
          <tr><th>Subject</th><td>${esc(e.subject || "(no subject)")}</td></tr>
          <tr><th>Date</th><td>${esc(fmtTs(e.internal_date_ms / 1000))}</td></tr>
          <tr><th>Verdict</th><td>${verdictPill(e.extraction_verdict)}</td></tr>
        </table>`));
      /* open-poll decisions live at the TOP — decide as soon as you've
         read enough, no scroll to the bottom required */
      const pollActionBar = (pl) => {
        const bar = h(`<div class="row" style="margin:0 0 8px;padding:9px 12px;background:var(--warn-bg);border:1px solid var(--line);border-radius:8px">
          <span style="font-size:12.5px"><b>#${pl.id}</b> ${esc(pl.candidate_name || "?")}${pl.proposed_status ? ` → <b>${esc(pl.proposed_status)}</b>` : ""}${pl.firm ? ` at ${esc(pl.firm)}` : ""}</span>
          <span style="margin-left:auto"></span>
          <button class="btn sm ok">✓ Approve</button>
          <button class="btn sm danger">✗ Reject</button>
        </div>`);
        const [bApprove, bReject] = bar.querySelectorAll("button");
        const act = (action) => {
          closeDrawer();
          queueAction({
            label: `${action === "approve" ? "Approving" : "Rejecting"} poll #${pl.id}`,
            url: `/admin/api/pipeline/poll/${pl.id}/${action}`,
            body: {},
            onCommit: (res) => {
              const ar = res.apply_result;
              if (ar && ar.ok === false) notify(`Poll #${pl.id}: applied with issues`, "bad", 6000);
              loadPolls(); load(); refreshCounts();
            },
          });
        };
        bApprove.onclick = () => act("approve");
        bReject.onclick = () => act("reject");
        return bar;
      };
      for (const pl of d.polls) {
        if (pl.status === "open") body.appendChild(pollActionBar(pl));
      }
      if (e.body_plain) body.appendChild(frag(`<h3>Body</h3><pre class="blob">${esc(e.body_plain)}</pre>`));
      if (e.extraction_json) body.appendChild(frag(`<h3>LLM extraction</h3><pre class="blob">${esc(JSON.stringify(e.extraction_json, null, 2))}</pre>`));
      for (const pl of d.polls) {
        body.appendChild(frag(`<h3>Poll #${pl.id} — ${esc(pl.poll_type)}</h3>
          <table class="kv">
            <tr><th>Status</th><td>${pollPill(pl.status)}</td></tr>
            <tr><th>Candidate</th><td>${esc(pl.candidate_name || "—")}</td></tr>
            <tr><th>Firm</th><td>${esc(pl.firm || "—")}</td></tr>
            ${pl.proposed_status ? `<tr><th>Proposed</th><td>${esc(pl.proposed_status)}</td></tr>` : ""}
            ${pl.resolved_by_name ? `<tr><th>Resolved by</th><td>${esc(pl.resolved_by_name)} · ${esc(fmtTs(anyTs(pl.resolved_at)))}</td></tr>` : ""}
          </table>`));
        if (pl.summary_md) body.appendChild(h(`<pre class="blob">${esc(pl.summary_md)}</pre>`));
        if (pl.apply_result) body.appendChild(frag(`<h3>Apply result</h3><pre class="blob">${esc(JSON.stringify(pl.apply_result, null, 2))}</pre>`));
      }
      for (const c of d.changes) {
        body.appendChild(frag(`<h3>Status change #${c.id}</h3>
          <table class="kv">
            <tr><th>Change</th><td><b>${esc(c.candidate_name)}</b> · ${esc(c.old_status || "?")} → <b>${esc(c.new_status)}</b> at ${esc(c.firm || "?")}</td></tr>
            <tr><th>Applied by</th><td>${esc(c.applied_by_name || "?")} · ${esc(fmtTs(anyTs(c.applied_at)))}</td></tr>
            <tr><th>Bitable</th><td>${c.bitable_record_id ? esc(c.bitable_record_id) : "not written"}</td></tr>
            <tr><th>Summary doc</th><td>${c.summary_doc_id ? esc(c.summary_doc_id) : "not written"}</td></tr>
          </table>`));
        if (c.notes) body.appendChild(h(`<pre class="blob">${esc(JSON.stringify(c.notes, null, 2))}</pre>`));
      }
      openDrawer(e.subject || "(no subject)", body);
    } catch (e2) { notify(e2.message, "bad", 5000); }
  }

  let deb;
  const debounced = () => { clearTimeout(deb); deb = setTimeout(() => load(), 350); };
  $("#pf-cand").oninput = debounced;
  $("#pf-firm").oninput = debounced;
  $("#pf-resolver").onchange = () => load();
  $("#pf-haspoll").onchange = () => load();
  $("#pf-more").onclick = () => load(true);

  state.viewKeys = (e) => {
    if (drawerEl && (e.key === "Escape")) { closeDrawer(); return true; }
    if (!rows.length) return false;
    if (e.key === "j") { cursor = Math.min(cursor + 1, rows.length - 1); paint(); $("tr.cursor", tbody)?.scrollIntoView({ block: "nearest" }); return true; }
    if (e.key === "k") { cursor = Math.max(cursor - 1, 0); paint(); $("tr.cursor", tbody)?.scrollIntoView({ block: "nearest" }); return true; }
    if (e.key === "Enter") { openEmail(rows[cursor]); return true; }
    if (e.key === "/") { $("#pf-cand").focus(); return true; }
    return false;
  };

  load();
}

/* ══ CANDIDATES WORKSPACE (Phase 5, read-only) ═══════════════════════ */

const artChip = (t, url) => {
  const label = { target_list: "TL", workup: "WU", firm_fit: "FF" }[t] || t;
  const title = { target_list: "Target list", workup: "Workup", firm_fit: "Firm fit" }[t] || t;
  return url
    ? `<a class="pill acc" href="${esc(url)}" target="_blank" rel="noopener" title="${esc(title)}" style="text-decoration:none">${esc(label)}</a>`
    : `<span class="pill dim" title="${esc(title)}">${esc(label)}</span>`;
};

const CRM_ORDER = ["Prospect", "Agreed to submit", "Submitted",
  "Interviewing", "Offer", "Hired", "Candidate Rejected the offer",
  "Candidate Accepted Another Offer", "Rejected by  Firm", "(no status)"];

function renderCandidates(el) {
  el.appendChild(h(`<div class="page-head">
    <h1>Candidates</h1>
    <span class="sub" id="cd-total">directory from the candidate folder tree</span>
  </div>`));

  /* ── live Bitable CRM board (source of truth for Status) ── */
  const crm = h(`<div class="panel"><div class="panel-head">
    <h2>Pipeline status · Bitable</h2>
    <label class="row" style="gap:6px;font-size:12px;color:var(--ink-2);cursor:pointer;margin-left:14px">
      <input type="checkbox" id="crm-stale"> Show stale</label>
    <span class="muted" id="crm-note" style="font-size:11.5px;margin-left:auto"></span></div>
    <div class="panel-body" id="crm-body"><span class="spin"></span></div></div>`);
  el.appendChild(crm);
  let crmRows = [], crmSel = null, showStale = false;
  $("#crm-stale", crm).onchange = (e) => { showStale = e.target.checked; paintCrm(); };

  async function loadCrm() {
    let d;
    try { d = await api("/admin/api/candidates/bitable"); }
    catch (e) { $("#crm-body", crm).innerHTML = `<span class="muted">${esc(e.message)}</span>`; return; }
    crmRows = d.rows;
    paintCrm();
    const nStale = d.rows.filter(r => r.statuses.includes("Stale")).length;
    $("#crm-note", crm).textContent = `${d.total} candidates (${nStale} stale) · refreshed ${fmtAgo(d.fetched_at)}`;
  }

  function paintCrm() {
    const active = showStale ? crmRows
      : crmRows.filter(r => !r.statuses.includes("Stale"));
    /* Submitted only counts activity in the last 6 months — real
       submission-doc date when we have one, Bitable last-modified as
       the fallback. Older ones get their own muted bucket. */
    const cutoff = Date.now() / 1000 - 183 * 86400;
    const bucketOf = (r) => {
      if (r.primary !== "Submitted") return r.primary;
      const ts = r.last_submission_ts || (r.modified_ms || 0) / 1000;
      return ts >= cutoff ? "Submitted" : "Submitted · >6mo";
    };
    const counts = {};
    for (const r of active) {
      const b = bucketOf(r);
      counts[b] = (counts[b] || 0) + 1;
    }
    const keys = [...CRM_ORDER.filter(k => counts[k]),
                  ...Object.keys(counts).filter(k => !CRM_ORDER.includes(k)).sort()];
    const body = $("#crm-body", crm);
    body.replaceChildren(h(`<div class="chips">
      ${keys.map(k => `<button class="chipstat ${crmSel === k ? "active" : ""}" data-st="${esc(k)}">
        <span>${esc(k)}</span><b>${counts[k]}</b></button>`).join("")}
    </div>`), h('<div id="crm-list"></div>'));
    body.querySelectorAll("[data-st]").forEach(b => b.onclick = () => {
      crmSel = crmSel === b.dataset.st ? null : b.dataset.st;
      paintCrm();
    });
    if (crmSel) {
      const members = active.filter(r => bucketOf(r) === crmSel);
      $("#crm-list", body).replaceChildren(h(`<div style="margin-top:10px">
        ${members.slice(0, 120).map(m => `<a href="#" data-cand="${esc(m.name.toLowerCase())}"
            class="pill dim" style="text-decoration:none;margin:0 4px 6px 0;display:inline-block">
            ${esc(m.name)}${m.statuses.length > 1 ? ` <span style="opacity:.6">· ${esc(m.statuses.slice(1).join(", "))}</span>` : ""}</a>`).join("")}
        ${members.length > 120 ? `<div class="muted" style="font-size:11.5px;margin-top:4px">+ ${members.length - 120} more — use search below</div>` : ""}
      </div>`));
      $("#crm-list", body).querySelectorAll("[data-cand]").forEach(a => a.onclick = (e) => {
        e.preventDefault(); openCandidate(a.dataset.cand);
      });
    }
  }
  loadCrm();
  const fbar = h(`<div class="row" style="margin:0 0 12px">
    <input class="input" id="cd-q" placeholder="Search name or practice…  ( / )" style="max-width:340px">
    <span class="muted" id="cd-count" style="margin-left:auto"></span>
  </div>`);
  const panel = h(`<div class="panel"><div class="panel-body flush">
    <table class="grid"><thead><tr>
      <th>Candidate</th><th>Practice</th><th>Bitable status</th><th>Docs</th>
      <th class="right">Submissions</th><th class="right">Pipeline</th><th>Last activity</th>
    </tr></thead><tbody></tbody></table>
  </div></div>`);
  el.append(fbar, panel);
  const tbody = $("tbody", panel);
  let rows = [], cursor = 0;

  async function load() {
    const q = $("#cd-q").value.trim();
    try {
      const d = await api(`/admin/api/candidates?q=${encodeURIComponent(q)}&limit=100`);
      rows = d.rows; cursor = 0;
      $("#cd-total").textContent = `${d.total} candidates with folders`;
      paint();
    } catch (e) { notify(e.message, "bad", 5000); }
  }

  function paint() {
    $("#cd-count").textContent = `${rows.length} shown`;
    tbody.replaceChildren(...rows.map((r, i) => {
      const tr = h(`<tr class="${i === cursor ? "cursor" : ""}" style="cursor:pointer">
        <td><b>${esc(r.name)}</b></td>
        <td><span class="pill dim">${esc(r.practice || "—")}</span></td>
        <td>${r.status ? `<span class="pill acc">${esc(r.status)}</span>` : '<span class="muted">—</span>'}</td>
        <td>${(r.artifacts || []).map(t => artChip(t)).join(" ") || '<span class="muted">—</span>'}</td>
        <td class="right">${r.submissions || "—"}</td>
        <td class="right">${r.pipeline_events || "—"}</td>
        <td class="muted nowrap">${r.last_activity ? fmtAgo(anyTs(r.last_activity)) : "—"}</td>
      </tr>`);
      tr.onclick = () => { cursor = i; paint(); openCandidate(r.key); };
      return tr;
    }));
    if (!rows.length)
      tbody.replaceChildren(h(`<tr><td colspan="7"><div class="empty-state"><div class="big">◦</div>No candidates match.</div></td></tr>`));
  }

  let deb;
  $("#cd-q").oninput = () => { clearTimeout(deb); deb = setTimeout(load, 300); };

  state.viewKeys = (e) => {
    if (drawerEl && e.key === "Escape") { closeDrawer(); return true; }
    if (!rows.length) return false;
    if (e.key === "j") { cursor = Math.min(cursor + 1, rows.length - 1); paint(); $("tr.cursor", tbody)?.scrollIntoView({ block: "nearest" }); return true; }
    if (e.key === "k") { cursor = Math.max(cursor - 1, 0); paint(); $("tr.cursor", tbody)?.scrollIntoView({ block: "nearest" }); return true; }
    if (e.key === "Enter") { openCandidate(rows[cursor].key); return true; }
    if (e.key === "/") { $("#cd-q").focus(); return true; }
    return false;
  };

  load();
  /* deep-link: #/candidates/<key> opens the drawer directly (palette) */
  const sub = location.hash.split("/").slice(2).join("/");
  if (sub) openCandidate(decodeURIComponent(sub));
}

async function openCandidate(key) {
  try {
    const d = await api(`/admin/api/candidates/detail?key=${encodeURIComponent(key)}`);
    const body = h("<div></div>");
    body.appendChild(frag(`<h3>Profile</h3><table class="kv">
      <tr><th>Practice</th><td>${esc(d.practice || "—")}</td></tr>
      <tr><th>Folder</th><td>${d.folder_url ? `<a href="${esc(d.folder_url)}" target="_blank" rel="noopener">${esc(d.folder_name || "open in Lark")}</a>` : esc(d.folder_name || "—")}</td></tr>
      ${Object.entries(d.subfolders || {}).map(([n, u]) => `<tr><th>· ${esc(n)}</th><td><a href="${esc(u)}" target="_blank" rel="noopener">open</a></td></tr>`).join("")}
      <tr><th>Bitable status</th><td>${d.bitable ? `<span class="pill acc">${esc(d.bitable.status || "?")}</span>` : '<span class="muted">not in Bitable yet</span>'}</td></tr>
    </table>`));
    if (d.artifacts?.length) {
      body.appendChild(frag(`<h3>Deliverables</h3><table class="kv">${d.artifacts.map(a => `
        <tr><th>${esc({ target_list: "Target list", workup: "Workup", firm_fit: "Firm fit" }[a.artifact_type] || a.artifact_type)}</th>
        <td><a href="${esc(a.doc_url)}" target="_blank" rel="noopener">${esc(a.doc_title || "open doc")}</a>
        <span class="muted"> · v${a.version}${anyTs(a.last_updated_at) ? ` · updated ${fmtAgo(anyTs(a.last_updated_at))}${a.last_updated_by_name ? ` by ${esc(a.last_updated_by_name)}` : ""}` : " · adopted from Drive"}</span></td></tr>`).join("")}
      </table>`));
    }
    if (d.submissions?.length) {
      body.appendChild(frag(`<h3>Submissions · ${d.submissions.length}</h3>
        <table class="kv">${d.submissions.map(s => `
          <tr><th class="nowrap">${esc(s.target_firm || "?")}</th>
          <td>${s.doc_url ? `<a href="${esc(s.doc_url)}" target="_blank" rel="noopener">doc</a> · ` : ""}${esc(s.target_office || "")} ${esc(s.seniority_bucket || "")}
          <span class="muted"> · ${s.doc_modify_time ? fmtAgo(anyTs(s.doc_modify_time)) : ""}</span>
          ${s.summary ? `<div class="muted" style="font-size:12px">${esc(s.summary)}</div>` : ""}</td></tr>`).join("")}
        </table>`));
    }
    if (d.pipeline?.length) {
      body.appendChild(frag(`<h3>Pipeline events</h3><table class="kv">${d.pipeline.map(pl => `
        <tr><th class="nowrap">${fmtAgo(anyTs(pl.created_at))}</th>
        <td>${pollPill(pl.status)} ${esc(pl.poll_type)} ${pl.proposed_status ? `→ <b>${esc(pl.proposed_status)}</b>` : ""} ${pl.firm ? `at ${esc(pl.firm)}` : ""}
        ${pl.resolved_by_name ? `<span class="muted"> · ${esc(pl.resolved_by_name)}</span>` : ""}</td></tr>`).join("")}
      </table>`));
    }
    if (d.emails?.length) {
      body.appendChild(frag(`<h3>Recent email mentions</h3><table class="kv">${d.emails.map(m => `
        <tr><th class="nowrap">${fmtAgo(m.internal_date_ms / 1000)}</th>
        <td>${verdictPill(m.extraction_verdict)} ${esc(m.subject || "(no subject)")}
        <div class="muted" style="font-size:11.5px">${esc(m.from_name || m.from_email || "")}</div></td></tr>`).join("")}
      </table>`));
    }
    openDrawer(d.name, body);
  } catch (e) { notify(e.message, "bad", 5000); }
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
          <div class="sub">agents-mac-mini → 127.0.0.1</div></div>
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
      any streaming card mid-render are dropped. Recruiters mid-conversation
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

/* ══ RECRUITERS WORKSPACE (Phase 7) ══════════════════════════════════ */

const fmtTok = (n) => !n ? "0" : n < 1e3 ? String(n) : n < 1e6 ? `${(n / 1e3).toFixed(1)}k` : `${(n / 1e6).toFixed(1)}M`;
const fmtUsd = (v) => !v ? "$0.00" : v < 1 ? `$${v.toFixed(3)}` : v < 100 ? `$${v.toFixed(2)}` : `$${Math.round(v).toLocaleString()}`;
const fmtMs = (ms) => ms == null ? "—" : ms < 1000 ? `${Math.round(ms)}ms` : `${(ms / 1000).toFixed(1)}s`;
const tokTotal = (u) => (u.input_tokens | 0) + (u.output_tokens | 0) + (u.cache_read | 0) + (u.cache_creation | 0);

/* 2px line + 10% wash + ringed endpoint dot; endpoint value labeled only. */
function sparkSvg(vals, w = 96, h = 26, labelEnd = false) {
  if (!vals || !vals.length || !vals.some(v => v)) return '<span class="muted">—</span>';
  const mx = Math.max(...vals), pad = 4;
  const lw = labelEnd ? 30 : 6;
  const step = (w - pad - lw) / Math.max(1, vals.length - 1);
  const pts = vals.map((v, i) =>
    [pad + i * step, h - pad - (v / mx) * (h - pad * 2)]);
  const line = "M " + pts.map(p => `${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(" L ");
  const [ex, ey] = pts[pts.length - 1];
  return `<svg viewBox="0 0 ${w} ${h}" width="${w}" height="${h}" role="img" aria-label="trend">
    <path class="spark-fill" d="${line} L ${ex.toFixed(1)},${h - pad} L ${pad},${h - pad} Z"/>
    <path class="spark-line" d="${line}"/>
    <circle class="spark-dot" cx="${ex.toFixed(1)}" cy="${ey.toFixed(1)}" r="4"/>
    ${labelEnd ? `<text class="spark-label" x="${ex + 7}" y="${Math.min(h - 3, ey + 4)}">${vals[vals.length - 1]}</text>` : ""}
  </svg>`;
}

function barRows(rows) {
  const mx = Math.max(...rows.map(r => r.v), 1);
  return rows.map(r => `<div class="barrow">
    <div class="lbl" title="${esc(r.label)}">${esc(r.label)}</div>
    <div class="track"><div class="fill" style="width:${Math.max(2, (r.v / mx) * 100)}%"></div></div>
    <div class="num">${esc(r.num)}</div>
  </div>`).join("");
}

function renderRecruiters(el) {
  el.appendChild(h(`<div class="page-head">
    <h1>Recruiters</h1><span class="sub">who uses Noto, how, and what it costs</span>
  </div>`));
  const root = h('<div><div class="empty-state"><span class="spin"></span></div></div>');
  el.appendChild(root);
  let users = [], cursor = 0;

  async function load() {
    let d;
    try { d = await api("/admin/api/recruiters?days=7"); }
    catch (e) { notify(e.message, "bad", 5000); return; }
    users = d.users; paint(d);
  }

  function paint(d) {
    const k = d.kpis || {};
    const dailyVals = (d.daily || []).map(x => x[1]);
    root.replaceChildren(h(`<div class="fade-in">
      <div class="tiles">
        <div class="tile"><div class="lbl">Messages · 7d</div>
          <div class="val">${(k.messages ?? 0).toLocaleString()}</div>
          <div class="sub">${k.active_users ?? 0} active recruiters</div></div>
        <div class="tile"><div class="lbl">Top workflow</div>
          <div class="val" style="font-size:17px">${esc((d.workflows?.[0]?.label) || k.top_workflow || "—")}</div>
          <div class="sub">${k.top_workflow_n ?? 0} invocations</div></div>
        <div class="tile"><div class="lbl">Tokens · 7d</div>
          <div class="val">${fmtTok(k.tokens_total | 0)}</div>
          <div class="sub">${fmtUsd(k.cost_usd)} on Claude</div></div>
        <div class="tile"><div class="lbl">Avg response</div>
          <div class="val">${fmtMs(d.avg_latency_ms)}</div>
          <div class="sub">across workflows · 7d</div></div>
      </div>

      <div class="panel"><div class="panel-head"><h2>Messages · last 14 days</h2></div>
        <div class="panel-body">${sparkSvg(dailyVals, 720, 64, true)}</div></div>

      <div class="panel"><div class="panel-head"><h2>Recruiters · full team from chat corpus</h2></div>
        <div class="panel-body flush"><table class="grid"><thead><tr>
          <th class="primary">Recruiter</th><th>Bot trend · 14d</th><th class="right">Bot · 7d</th>
          <th class="right">Chat · 7d</th><th class="right">Chat · 30d</th><th class="right">Tokens</th>
          <th class="right">Cost</th><th class="right">Latency</th><th>Last seen</th>
        </tr></thead><tbody id="rc-body"></tbody></table></div></div>

      <div class="panel"><div class="panel-head"><h2>Workflows · 7d</h2></div>
        <div class="panel-body" id="rc-wf"></div></div>
    </div>`));

    const tbody = $("#rc-body", root);
    tbody.replaceChildren(...users.map((u, i) => {
      const lastAny = Math.max(u.last_seen || 0, (u.chat_last_ms || 0) / 1000);
      const tr = h(`<tr class="${i === cursor ? "cursor" : ""}" style="cursor:pointer">
        <td class="primary"><b>${esc(u.display_name || "Unknown")}</b><div class="oid mono muted" style="font-size:10.5px">${esc(u.open_id)}</div></td>
        <td>${sparkSvg(u.spark || [], 96, 24)}</td>
        <td class="right"><b>${u.msgs_window ?? 0}</b></td>
        <td class="right">${u.chat_msgs_7d ?? "—"}</td>
        <td class="right">${u.chat_msgs_30d ?? "—"}</td>
        <td class="right">${fmtTok(tokTotal(u))}</td>
        <td class="right">${fmtUsd(u.cost_usd)}</td>
        <td class="right">${fmtMs(u.latency?.avg_ms)}</td>
        <td class="muted nowrap">${lastAny ? fmtAgo(lastAny) : "—"}</td>
      </tr>`);
      tr.onclick = () => { cursor = i; openRecruiter(u.open_id); };
      return tr;
    }));
    if (!users.length)
      tbody.replaceChildren(h('<tr><td colspan="8"><div class="empty-state">No recruiter activity recorded yet.</div></td></tr>'));

    $("#rc-wf", root).innerHTML = (d.workflows || []).length
      ? barRows(d.workflows.map(w => ({
          label: w.label || w.workflow, v: w.messages || 0,
          num: `${w.messages} · ${fmtTok(w.tokens | 0)} · ${fmtUsd(w.cost_usd)}` })))
      : '<span class="muted">no workflow invocations in window</span>';
  }

  async function openRecruiter(oid) {
    try {
      const d = await api(`/admin/api/recruiters/detail?oid=${encodeURIComponent(oid)}`);
      const u = d.user || {};
      const body = h("<div></div>");
      body.appendChild(frag(`<h3>Activity · ${d.days}d</h3>
        <div style="margin:0 0 10px">${sparkSvg((d.daily || []).map(x => x[1]), 540, 56, true)}</div>
        <table class="kv">
          <tr><th>Messages</th><td>${u.msg_count ?? "?"} total · first seen ${u.first_seen ? fmtAgo(u.first_seen) : "?"}</td></tr>
          <tr><th>Avg response</th><td>${fmtMs(d.latency?.avg_ms)} over ${d.latency?.n ?? 0} runs</td></tr>
        </table>`));
      if (d.actions?.length) {
        body.appendChild(frag(`<h3>Use cases · 30d</h3><div>${barRows(d.actions.map(a => ({
          label: a.action_type.replace(/_/g, " "), v: a.n,
          num: String(a.n) })))}</div>`));
      } else {
        body.appendChild(frag(`<h3>Use cases · 30d</h3>
          <p class="muted" style="font-size:12px">no classified requests yet — accrues after the next bot restart</p>`));
      }
      if (d.workflows?.length) {
        body.appendChild(frag(`<h3>Workflows</h3><div>${barRows(d.workflows.map(w => ({
          label: w.label || w.workflow, v: w.messages || 0,
          num: `${w.messages} · ${fmtUsd(w.cost_usd)}` })))}</div>`));
      }
      if (d.memory) {
        if (d.memory.error) {
          body.appendChild(frag(`<h3>Memory</h3><p class="muted">${esc(d.memory.error)}</p>`));
        } else {
          body.appendChild(frag(`<h3>Recruiter memory · super_admin only</h3>
            <p class="muted" style="font-size:11.5px;margin:0 0 8px">DM-derived personal context — never shown to members or in any group surface.</p>`));
          for (const f of (d.memory.facts || [])) {
            const md = f.metadata || {};
            body.appendChild(h(`<div class="panel" style="margin:0 0 8px"><div class="panel-body">
              <div class="row"><b class="mono" style="font-size:12px">${esc(f.slug)}</b>
                <span class="pill dim">${esc(md.type || "")}</span>
                <span class="muted" style="font-size:11px;margin-left:auto">×${md.reinforcement_count ?? 1} · ${fmtAgo(anyTs(md.updated_at))}</span></div>
              <div class="dim" style="font-size:12.5px;margin-top:4px">${esc(f.description || "")}</div>
            </div></div>`));
          }
          if (!(d.memory.facts || []).length)
            body.appendChild(h('<p class="muted">no facts learned yet</p>'));
        }
      }
      openDrawer(u.display_name || oid, body);
    } catch (e) { notify(e.message, "bad", 5000); }
  }

  state.viewKeys = (e) => {
    if (drawerEl && e.key === "Escape") { closeDrawer(); return true; }
    if (!users.length) return false;
    if (e.key === "j") { cursor = Math.min(cursor + 1, users.length - 1); paintCursor(); return true; }
    if (e.key === "k") { cursor = Math.max(cursor - 1, 0); paintCursor(); return true; }
    if (e.key === "Enter") { openRecruiter(users[cursor].open_id); return true; }
    return false;
  };
  function paintCursor() {
    [...(root.querySelector("#rc-body")?.children || [])].forEach((tr, i) =>
      tr.classList.toggle("cursor", i === cursor));
  }

  load();
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
    try { d = await api("/admin/api/recruiters?days=7"); }
    catch (e) { notify(e.message, "bad", 5000); return; }
    if (!(d.actions || []).length) {
      root.replaceChildren(h(`<div class="panel"><div class="empty-state">
        <div class="big">◌</div>
        <div><b>No use-case data yet.</b></div>
        <div style="margin-top:6px;max-width:520px;margin-left:auto;margin-right:auto">
          Every message gets classified by the skill Noto used for it —
          submission draft/edit, target list, workup, firm fit, research
          question, doc edits, commands — starting from the next bot
          restart. Within a week or two this page shows the team's most
          common use cases and who's using (or missing) each one.</div>
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
          num: `${a.messages} · ${a.users} recruiter(s)` })))}</div></div>
      <div class="panel"><div class="panel-head"><h2>Coverage by recruiter · 30d</h2>
        <span class="muted" style="font-size:11.5px;margin-left:auto">— means never used it: your "did you know Noto can…" list</span></div>
        <div class="panel-body flush" style="overflow-x:auto"><table class="grid"><thead><tr>
          <th class="primary">Recruiter</th>${topActs.map(t => `<th class="right">${esc(t.replace(/_/g, " "))}</th>`).join("")}
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
