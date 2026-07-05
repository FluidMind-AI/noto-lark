#!/usr/bin/env python3
"""
Operator dashboard for Noto — per-user, filterable, taskful.

A single-page admin surface for Alejandro (the operator). Designed in the
"product" register: design SERVES the task, not the other way around.
The job is one glance + one drill-down: "who is using Noto, how, and
what is it costing me?" — and "filter to this user to see what
they're doing and saying back to it."

Sections (top to bottom):

  - Filter bar       — sticky; "All users" or a single chip with ×
                       to clear. Every other user is a click away.
  - Hero KPIs        — 6 tiles. Volume, attention, top workflow, spend,
                       OAuth state, oldest corpus source.
  - Users table — name, msgs L7d, L30d, workflows used, tokens L7d,
                       cost L7d, last seen. Names are filter links.
  - Workflows        — what's being asked + by how many distinct users
                       + what it cost. Filtered when a user is selected.
  - Token spend      — daily sparkline + per-workflow breakdown.
  - Feedback         — open / accepted / rejected; filterable by user.
  - Corpus           — sorted by staleness. The OLDEST source is the
                       bottleneck — sort puts it on top.
  - OAuth / tokens   — auth health.
  - Roadmap          — engineering backlog + Eisenhower.

URL contract:
  /dashboard?key=<SECRET>                 — global view
  /dashboard?key=...&user=<open_id>       — drill into one user
  /dashboard?key=...&sort=<key>           — sort the users table

CLI:
  python tools/dashboard.py > out.html
  python tools/dashboard.py --user ou_xxx > out.html
"""

import argparse
import html
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import load_config, get_home, get_path  # noqa: E402


WINDOW_DAYS = 7                          # rolling window the dashboard tracks
DISPLAY_TZ = "Asia/Singapore"

# Sort keys the users table accepts (column -> SQL fragment).
_SORT_KEYS = {
    "msgs":     "msgs_window DESC",
    "msgs30":   "msgs_30d DESC",
    "tokens":   "input_tokens + output_tokens + cache_read + cache_creation DESC",
    "cost":     "cost_usd DESC",
    "last":     "last_seen DESC",
    "name":     "display_name ASC",
}
_DEFAULT_SORT = "msgs"


# ---------------------------------------------------------------------------
# Time + formatting helpers
# ---------------------------------------------------------------------------

def _now() -> float:
    return time.time()


def _to_epoch(x: Any) -> Optional[float]:
    """Normalise epoch float OR ISO-8601 string into epoch. Returns None on
    missing/invalid; callers branch on None."""
    if x is None or x == "":
        return None
    if isinstance(x, (int, float)):
        try:
            v = float(x)
            return v if v > 0 else None
        except (TypeError, ValueError):
            return None
    if isinstance(x, str):
        try:
            dt = datetime.fromisoformat(x.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except (TypeError, ValueError):
            return None
    return None


def _ago(epoch: Any, now: Optional[float] = None) -> str:
    ep = _to_epoch(epoch)
    if ep is None:
        return "never"
    now = now if now is not None else _now()
    s = max(0.0, now - ep)
    if s < 60:
        return f"{int(s)}s ago"
    if s < 3600:
        return f"{int(s // 60)}m ago"
    if s < 86400:
        return f"{int(s // 3600)}h ago"
    return f"{int(s // 86400)}d ago"


def _fmt_local(epoch: Any) -> str:
    ep = _to_epoch(epoch)
    if ep is None:
        return "—"
    try:
        from zoneinfo import ZoneInfo
        return datetime.fromtimestamp(ep, ZoneInfo(DISPLAY_TZ)) \
            .strftime("%Y-%m-%d %H:%M %Z")
    except Exception:
        return datetime.utcfromtimestamp(ep).strftime("%Y-%m-%d %H:%M UTC")


def _mtime(path: str) -> Optional[float]:
    try:
        return os.path.getmtime(path) if os.path.exists(path) else None
    except OSError:
        return None


def _fresh_class(epoch: Any, fresh_s: int = 24 * 3600,
                 amber_s: int = 7 * 24 * 3600) -> str:
    ep = _to_epoch(epoch)
    if ep is None:
        return "stale-red"
    age = _now() - ep
    if age < fresh_s:
        return "stale-green"
    if age < amber_s:
        return "stale-amber"
    return "stale-red"


def _fmt_int(n: Optional[int]) -> str:
    if n is None:
        return "—"
    return f"{int(n):,}"


def _fmt_tokens(n: int) -> str:
    """Compact token render — 1.2k / 45k / 1.3M."""
    if n is None or n == 0:
        return "0"
    if n < 1_000:
        return f"{n}"
    if n < 1_000_000:
        return f"{n / 1_000:.1f}k"
    return f"{n / 1_000_000:.1f}M"


def _fmt_money(usd: Optional[float]) -> str:
    if not usd:
        return "$0.00"
    if usd < 1:
        return f"${usd:.3f}"
    if usd < 100:
        return f"${usd:.2f}"
    return f"${usd:,.0f}"


def _esc(x: Any) -> str:
    return html.escape(str(x if x is not None else ""))


def _short(open_id: str, n: int = 6) -> str:
    """Last n chars of an open_id, prefixed with a bullet — a stable,
    visually-quiet handle when no display name is set."""
    if not open_id:
        return "?"
    return "·" + open_id[-n:]


def _display(open_id: str, name: Optional[str]) -> str:
    if name and name.strip() and name.strip() != open_id:
        return name.strip()
    # Unknown: show open_id suffix so duplicates are still distinguishable.
    return f"Unknown {_short(open_id)}"


def _read_text(path: str, limit: int = 200_000) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read(limit)
    except Exception:
        return ""


def _markdown_lite(text: str) -> str:
    """Tiny markdown subset (h/p/ul). Operator-written content is well-
    behaved; we deliberately don't pull in a markdown lib."""
    if not text:
        return '<p class="muted">empty</p>'
    out, in_ul = [], False
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            if in_ul:
                out.append("</ul>"); in_ul = False
            continue
        if line.startswith("### "):
            if in_ul:
                out.append("</ul>"); in_ul = False
            out.append(f"<h4>{_esc(line[4:])}</h4>"); continue
        if line.startswith("## "):
            if in_ul:
                out.append("</ul>"); in_ul = False
            out.append(f"<h3>{_esc(line[3:])}</h3>"); continue
        if line.startswith("# "):
            if in_ul:
                out.append("</ul>"); in_ul = False
            out.append(f"<h3>{_esc(line[2:])}</h3>"); continue
        if line.lstrip().startswith(("- ", "* ")):
            if not in_ul:
                out.append("<ul>"); in_ul = True
            out.append(f"<li>{_esc(line.lstrip()[2:])}</li>"); continue
        if in_ul:
            out.append("</ul>"); in_ul = False
        out.append(f"<p>{_esc(line)}</p>")
    if in_ul:
        out.append("</ul>")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# URL helpers — every link respects the existing filter + key
# ---------------------------------------------------------------------------

class State:
    """Carries the request parameters every renderer needs."""
    def __init__(self, key: str = "", user: Optional[str] = None,
                 sort: Optional[str] = None):
        self.key = key
        self.user = user
        self.sort = sort if sort in _SORT_KEYS else _DEFAULT_SORT

    def url(self, **overrides) -> str:
        params: Dict[str, str] = {}
        if self.key:
            params["key"] = self.key
        if self.user is not None and "user" not in overrides:
            params["user"] = self.user
        if self.sort and self.sort != _DEFAULT_SORT and "sort" not in overrides:
            params["sort"] = self.sort
        for k, v in overrides.items():
            if v is None:
                params.pop(k, None)
            else:
                params[k] = str(v)
        return "/dashboard" + ("?" + urlencode(params) if params else "")


# ---------------------------------------------------------------------------
# Bot process + git inspection (subprocess, no bot import)
# ---------------------------------------------------------------------------

def _bot_pid_uptime() -> Tuple[Optional[str], Optional[str]]:
    """The dashboard IS the bot — we render in the bot's own process,
    so os.getpid() directly gives the bot's PID. No pgrep needed.

    Earlier versions of this function used `pgrep -f "lark_bot.py serve"`
    but that returns nothing when pgrep is launched FROM the bot itself
    (macOS pgrep excludes its ancestry), so the dashboard always showed
    "bot not running" — even when the bot was very obviously serving
    the dashboard request. os.getpid() is exact, instant, and right
    by construction.

    CLI rendering (`python tools/dashboard.py > out.html`) shows the
    CLI process's PID + etime instead — also correct semantics
    ("PID of whatever rendered this page")."""
    pid = str(os.getpid())
    try:
        et = subprocess.run(["ps", "-p", pid, "-o", "etime="],
                            capture_output=True, text=True, timeout=2)
        return pid, (et.stdout.strip() or None)
    except Exception:
        return pid, None


def _git_branch_commit() -> Tuple[str, str]:
    try:
        b = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                           capture_output=True, text=True, cwd=get_home(),
                           timeout=2).stdout.strip()
        c = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                           capture_output=True, text=True, cwd=get_home(),
                           timeout=2).stdout.strip()
        return b or "?", c or "?"
    except Exception:
        return "?", "?"


def _bot_stats() -> Dict[str, Any]:
    path = os.path.join(get_home(), "lark", "bot_stats.json")
    try:
        with open(path) as f:
            return json.load(f) or {}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Section: filter bar (sticky)
# ---------------------------------------------------------------------------

def section_filter_bar(state: State, users: List[Dict[str, Any]],
                       active_user: Optional[Dict[str, Any]]) -> str:
    """Active user chip (with × to clear) on the left; the rest of
    the users as quiet links so a manager can hop between any of
    them in one click."""
    if active_user:
        name = _display(active_user["open_id"], active_user.get("display_name"))
        chip = (
            f'<a class="chip-active" href="{_esc(state.url(user=None))}"'
            f' title="Clear filter">{_esc(name)}'
            f' <span class="chip-close">×</span></a>'
        )
        others = [u for u in users if u["open_id"] != active_user["open_id"]]
        sub = "Filtering everything below to this user."
    else:
        chip = '<span class="chip-all">All users</span>'
        others = users
        sub = ("Click a user below to drill in. "
               "Every section will filter to just them.")
    other_links = ""
    if others:
        other_links = " &nbsp;·&nbsp; ".join(
            f'<a href="{_esc(state.url(user=u["open_id"]))}" '
            f'title="{_esc(u["open_id"])}">'
            f'{_esc(_display(u["open_id"], u.get("display_name")))}</a>'
            for u in others[:10]
        )
    return (
        '<div class="filter-bar">'
        f'<div class="filter-current">{chip}</div>'
        f'<div class="filter-sub">{sub}</div>'
        f'<div class="filter-others">{other_links}</div>'
        '</div>'
    )


# ---------------------------------------------------------------------------
# Section: hero KPIs (6 tiles)
# ---------------------------------------------------------------------------

def section_hero(state: State, kpis: Dict[str, Any],
                 corpus_oldest: Optional[Tuple[str, float]],
                 corpus_newest: Optional[Tuple[str, float]],
                 oauth: Dict[str, Any]) -> str:
    def tile(label: str, value: str, sub: str = "", klass: str = "") -> str:
        return (
            f'<div class="tile {klass}">'
            f'<div class="tile-label">{_esc(label)}</div>'
            f'<div class="tile-value">{value}</div>'
            f'<div class="tile-sub">{_esc(sub)}</div>'
            '</div>'
        )

    top_wf = kpis.get("top_workflow") or "—"
    from usage_store import WORKFLOWS as _WF
    top_wf_label = _WF.get(top_wf, top_wf) if top_wf and top_wf != "—" else "—"

    # Hero shows the NEWEST refresh ("is anything refreshing at all?").
    # The bottleneck (oldest, stale source) lives in the corpus table
    # below with the red BOTTLENECK tag — separate concern, different
    # question. Showing oldest here made the dashboard look "stale" even
    # when the nightly resync was running fine 6h ago.
    newest_label = "—"
    newest_klass = "stale-red"
    newest_age = "—"
    if corpus_newest:
        newest_label = corpus_newest[0]
        newest_klass = _fresh_class(corpus_newest[1])
        newest_age = _ago(corpus_newest[1])

    oauth_label = "—"
    oauth_klass = "stale-red"
    oauth_sub = ""
    if oauth.get("access_token_valid"):
        oauth_klass = "stale-green"
        oauth_label = "OK"
        oauth_sub = f"refreshes in {oauth.get('refresh_expires_in_days', 0):.0f}d"
    elif oauth.get("authorized"):
        oauth_klass = "stale-amber"
        oauth_label = "expired"
        oauth_sub = "keepalive missed"
    else:
        oauth_label = "not authorized"

    # Active drafting sessions count (from submission_sessions) — a useful
    # "right now" signal: how many users are mid-flight on a draft.
    active_sessions = 0
    sub_db = os.path.join(get_home(), "indexes", "submissions.db")
    try:
        if os.path.exists(sub_db):
            db = sqlite3.connect(sub_db)
            row = db.execute(
                "SELECT COUNT(*) FROM submission_sessions "
                "WHERE last_activity_at >= ?",
                (_now() - 3 * 86400,),
            ).fetchone()
            active_sessions = int(row[0]) if row else 0
            db.close()
    except Exception:
        pass

    tiles = [
        tile(f"Messages · L{WINDOW_DAYS}d",
             _fmt_int(kpis.get("messages")),
             f"{_fmt_int(kpis.get('active_users'))} active users"),
        tile("Top workflow",
             _esc(top_wf_label),
             f"{_fmt_int(kpis.get('top_workflow_n'))} invocations"),
        tile(f"Tokens · L{WINDOW_DAYS}d",
             _fmt_tokens(int(kpis.get("tokens_total") or 0)),
             f"{_fmt_money(kpis.get('cost_usd'))} on Claude"),
        tile("OAuth", _esc(oauth_label),
             oauth_sub, oauth_klass),
        tile("Latest corpus refresh",
             _esc(newest_age),
             _esc(newest_label), newest_klass),
        tile("Active drafts",
             _fmt_int(active_sessions),
             "sessions touched in last 3 days"),
    ]
    return f'<section class="hero">{"".join(tiles)}</section>'


# ---------------------------------------------------------------------------
# Section: users table
# ---------------------------------------------------------------------------

def section_users(state: State, users: List[Dict[str, Any]]) -> str:
    real_users = [u for u in users
                  if not str(u.get("open_id", "")).startswith("ou_test")
                  and not str(u.get("open_id", "")).startswith("ou_smoke")]
    if not real_users:
        return _card(
            "Users",
            '<p class="empty">No real user activity yet (the test rows '
            'in <code>usage.db</code> were added during instrumentation '
            'smoke-tests and aren’t shown here). Once the team starts '
            'DMing the bot or @mentioning it in groups, names will appear '
            'with their message and token totals.</p>'
        )
    users = real_users

    def th(label: str, key: Optional[str], num: bool = False) -> str:
        klass = "num" if num else ""
        if not key:
            return f'<th class="{klass}">{_esc(label)}</th>'
        active = state.sort == key
        arrow = ' <span class="arrow">↓</span>' if active else ""
        href = state.url(sort=key)
        return (f'<th class="{klass}{" sorted" if active else ""}">'
                f'<a href="{_esc(href)}">{_esc(label)}{arrow}</a></th>')

    rows_html = []
    for u in users:
        name = _display(u["open_id"], u.get("display_name"))
        is_active = (state.user == u["open_id"])
        href = state.url(user=u["open_id"])
        tok_total = (int(u.get("input_tokens") or 0)
                     + int(u.get("output_tokens") or 0)
                     + int(u.get("cache_read") or 0)
                     + int(u.get("cache_creation") or 0))
        rows_html.append(
            '<tr' + (' class="active"' if is_active else '') + '>'
            f'<td class="name"><a href="{_esc(href)}">{_esc(name)}</a>'
            f'<div class="oid">{_esc(u["open_id"])}</div></td>'
            f'<td class="num strong">{_fmt_int(u.get("msgs_window"))}</td>'
            f'<td class="num">{_fmt_int(u.get("msgs_30d"))}</td>'
            f'<td class="num">{_fmt_int(u.get("workflows_used"))}</td>'
            f'<td class="num">{_fmt_tokens(tok_total)}</td>'
            f'<td class="num">{_fmt_money(u.get("cost_usd"))}</td>'
            f'<td class="muted small">{_esc(_ago(u.get("last_seen")))}</td>'
            '</tr>'
        )

    table = (
        '<table class="data sortable"><thead><tr>'
        f'{th("User", "name")}'
        f'{th(f"L{WINDOW_DAYS}d msgs", "msgs", True)}'
        f'{th("L30d msgs", "msgs30", True)}'
        f'{th("Workflows", None, True)}'
        f'{th(f"L{WINDOW_DAYS}d tokens", "tokens", True)}'
        f'{th(f"L{WINDOW_DAYS}d cost", "cost", True)}'
        f'{th("Last seen", "last")}'
        '</tr></thead><tbody>'
        + "".join(rows_html) +
        '</tbody></table>'
    )
    sort_labels = {
        "msgs":   f"L{WINDOW_DAYS}d messages",
        "msgs30": "L30d messages",
        "tokens": f"L{WINDOW_DAYS}d tokens",
        "cost":   f"L{WINDOW_DAYS}d cost",
        "last":   "most recent activity",
        "name":   "name",
    }
    title = (f"Users · sorted by {sort_labels.get(state.sort, state.sort)}"
             + (f" · {len(users)} total" if len(users) > 0 else ""))
    return _card(title, table)


# ---------------------------------------------------------------------------
# Section: workflows breakdown
# ---------------------------------------------------------------------------

def section_workflows(state: State, rows: List[Dict[str, Any]]) -> str:
    title = ("Workflows" + (
        " · filtered to this user" if state.user else " · all users"))
    if not rows:
        return _card(title,
                     '<p class="empty">No workflow invocations recorded in '
                     f'the last {WINDOW_DAYS} days{" for this user" if state.user else ""}. '
                     'Send a message to the bot to populate this table.</p>')
    body = ['<table class="data"><thead><tr>'
            '<th>Workflow</th>'
            '<th class="num">Invocations</th>'
            '<th class="num">Distinct users</th>'
            '<th class="num">Tokens</th>'
            '<th class="num">Cost</th>'
            '</tr></thead><tbody>']
    for r in rows:
        users_cell = ("—" if state.user
                      else _fmt_int(r.get("users")))
        body.append(
            '<tr>'
            f'<td>{_esc(r["label"])}<div class="muted small mono">'
            f'{_esc(r["workflow"])}</div></td>'
            f'<td class="num strong">{_fmt_int(r.get("messages"))}</td>'
            f'<td class="num">{users_cell}</td>'
            f'<td class="num">{_fmt_tokens(int(r.get("tokens") or 0))}</td>'
            f'<td class="num">{_fmt_money(r.get("cost_usd"))}</td>'
            '</tr>'
        )
    body.append('</tbody></table>')
    return _card(title, "".join(body))


# ---------------------------------------------------------------------------
# Section: token spend (sparkline + per-workflow split)
# ---------------------------------------------------------------------------

def section_tokens(state: State, daily: List[Tuple[str, int]],
                   wf_rows: List[Dict[str, Any]]) -> str:
    spark = _sparkline(daily)
    total = sum(n for _, n in daily)
    today = daily[-1][1] if daily else 0
    avg = total / max(1, len(daily))

    by_wf_rows = "".join(
        f'<tr><td>{_esc(r["label"])}</td>'
        f'<td class="num">{_fmt_tokens(int(r.get("tokens") or 0))}</td>'
        f'<td class="num">{_fmt_money(r.get("cost_usd"))}</td></tr>'
        for r in wf_rows if (r.get("tokens") or 0) > 0
    ) or '<tr><td colspan="3" class="muted">no token usage in window</td></tr>'

    return _card(
        "Token spend",
        '<div class="split">'
        '<div class="split-l">'
        '<div class="muted small">Messages · last 14 days · '
        f'today {today} · avg/day {avg:.1f}</div>'
        f'<div class="spark">{spark}</div>'
        '</div>'
        '<div class="split-r">'
        '<table class="data"><thead><tr>'
        '<th>Workflow</th><th class="num">Tokens</th><th class="num">Cost</th>'
        '</tr></thead><tbody>'
        f'{by_wf_rows}'
        '</tbody></table>'
        '</div>'
        '</div>'
    )


def _sparkline(daily: List[Tuple[str, int]], w: int = 320, h: int = 56) -> str:
    if not daily:
        return ""
    vals = [n for _, n in daily]
    mx = max(vals) or 1
    n = len(vals)
    step = w / max(1, n - 1)
    pts = []
    for i, v in enumerate(vals):
        x = i * step
        y = h - 4 - (v / mx) * (h - 8)
        pts.append(f"{x:.1f},{y:.1f}")
    path_line = "M " + " L ".join(pts)
    path_fill = (path_line + f" L {pts[-1].split(',')[0]},{h}"
                 f" L 0,{h} Z")
    # Tick labels: only first + last date (don't compete with the line)
    first_d = daily[0][0][-5:]                # MM-DD
    last_d = daily[-1][0][-5:]
    return (
        f'<svg viewBox="0 0 {w} {h+18}" xmlns="http://www.w3.org/2000/svg" '
        f'class="spark-svg" width="100%" height="auto">'
        f'<path d="{path_fill}" class="spark-fill"/>'
        f'<path d="{path_line}" class="spark-line"/>'
        f'<text x="0" y="{h+14}" class="spark-label">{first_d}</text>'
        f'<text x="{w}" y="{h+14}" class="spark-label" '
        f'text-anchor="end">{last_d}</text>'
        f'</svg>'
    )


# ---------------------------------------------------------------------------
# Section: feedback (filterable by user)
# ---------------------------------------------------------------------------

def _fb_db() -> Optional[str]:
    p = os.path.join(get_home(), "indexes", "feedback.db")
    return p if os.path.exists(p) else None


def section_feedback(state: State) -> str:
    p = _fb_db()
    if not p:
        return _card("Feedback", '<p class="empty">No feedback DB yet.</p>')
    cutoff = _now() - WINDOW_DAYS * 86400
    user_filter = " AND user_open_id = ?" if state.user else ""
    user_params = [state.user] if state.user else []

    db = sqlite3.connect(p)
    db.row_factory = sqlite3.Row
    try:
        open_rows = db.execute(
            f"SELECT id, user_name, workflow, kind, feedback_text, created_at "
            f"FROM feedback WHERE status='open'{user_filter} "
            "ORDER BY created_at DESC LIMIT 20", user_params,
        ).fetchall()
        open_by_kind = db.execute(
            f"SELECT COALESCE(kind,'(none)') AS k, COUNT(*) AS n "
            f"FROM feedback WHERE status='open'{user_filter} "
            "GROUP BY k ORDER BY n DESC", user_params,
        ).fetchall()
        open_by_wf = db.execute(
            f"SELECT COALESCE(workflow,'(none)') AS w, COUNT(*) AS n "
            f"FROM feedback WHERE status='open'{user_filter} "
            "GROUP BY w ORDER BY n DESC", user_params,
        ).fetchall()
        accepted = db.execute(
            f"SELECT user_name, workflow, kind, feedback_text, "
            f"  resolved_at, resolution_note "
            f"FROM feedback WHERE status='accepted'{user_filter} "
            f"  AND COALESCE(resolved_at, created_at) >= ? "
            "ORDER BY COALESCE(resolved_at, created_at) DESC LIMIT 10",
            user_params + [cutoff],
        ).fetchall()
        rejected = db.execute(
            f"SELECT user_name, workflow, kind, feedback_text, "
            f"  resolved_at, resolution_note "
            f"FROM feedback WHERE status='rejected'{user_filter} "
            f"  AND COALESCE(resolved_at, created_at) >= ? "
            "ORDER BY COALESCE(resolved_at, created_at) DESC LIMIT 10",
            user_params + [cutoff],
        ).fetchall()
    finally:
        db.close()

    def _chips(rows):
        if not rows:
            return '<p class="muted small">none</p>'
        return ('<div class="chips">'
                + "".join(f'<span class="chip">{_esc(k)}'
                          f'<b>{_esc(n)}</b></span>'
                          for (k, n) in rows)
                + '</div>')

    def _fb_item(user, wf, kind, text, when, note=None):
        head = (
            f'<span class="who">{_esc(user or "?")}</span>'
            f' <span class="chip-i">{_esc(wf or "?")}</span>'
            f' <span class="chip-i">{_esc(kind or "?")}</span>'
            f' <span class="muted small">· {_esc(_ago(when))}</span>'
        )
        body = f'<div class="fb-text">{_esc(text or "")}</div>'
        if note:
            body += f'<div class="muted small">→ {_esc(note)}</div>'
        return f'<li>{head}{body}</li>'

    open_list = "".join(
        _fb_item(r["user_name"], r["workflow"], r["kind"],
                 r["feedback_text"], r["created_at"])
        for r in open_rows
    ) or ('<li class="muted">Nothing open — every piece of feedback '
          'is resolved.</li>')

    accepted_list = "".join(
        _fb_item(r["user_name"], r["workflow"], r["kind"],
                 r["feedback_text"], r["resolved_at"], r["resolution_note"])
        for r in accepted
    ) or '<li class="muted">No accepted feedback in window.</li>'

    rejected_list = "".join(
        _fb_item(r["user_name"], r["workflow"], r["kind"],
                 r["feedback_text"], r["resolved_at"], r["resolution_note"])
        for r in rejected
    ) or '<li class="muted">No rejected feedback in window.</li>'

    return _card(
        "Feedback" + (" · this user" if state.user else ""),
        '<div class="split">'
        '<div class="split-l">'
        f'<h4>Unresolved · {len(open_rows)}</h4>'
        f'<div class="muted small">by kind</div>{_chips([(r["k"], r["n"]) for r in open_by_kind])}'
        f'<div class="muted small">by workflow</div>{_chips([(r["w"], r["n"]) for r in open_by_wf])}'
        f'<ul class="fb">{open_list}</ul>'
        '</div>'
        '<div class="split-r">'
        f'<h4>Accepted · last {WINDOW_DAYS}d</h4>'
        f'<ul class="fb">{accepted_list}</ul>'
        f'<h4>Rejected · last {WINDOW_DAYS}d</h4>'
        f'<ul class="fb">{rejected_list}</ul>'
        '</div></div>'
    )


# ---------------------------------------------------------------------------
# Section: corpus bottleneck (sorted by staleness; oldest first)
# ---------------------------------------------------------------------------

def _max_synced(db_path: str, table: str) -> Optional[float]:
    if not os.path.exists(db_path):
        return None
    try:
        db = sqlite3.connect(db_path)
        try:
            row = db.execute(f"SELECT MAX(synced_at) FROM {table}").fetchone()
        finally:
            db.close()
        return _to_epoch(row[0]) if row else None
    except Exception:
        return None


def _doc_index_counts(db_path: str) -> Tuple[Optional[int], Optional[int]]:
    if not os.path.exists(db_path):
        return None, None
    try:
        db = sqlite3.connect(db_path)
        try:
            chunks = db.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
            arts = db.execute(
                "SELECT COUNT(DISTINCT doc_id) FROM docs").fetchone()[0]
        finally:
            db.close()
        return int(chunks), int(arts)
    except Exception:
        return None, None


def _wiki_oldest(wiki_dir: str) -> Optional[float]:
    if not os.path.isdir(wiki_dir):
        return None
    oldest = None
    for root, _d, files in os.walk(wiki_dir):
        for f in files:
            if not f.endswith(".md"):
                continue
            m = _mtime(os.path.join(root, f))
            if m is None:
                continue
            if oldest is None or m < oldest:
                oldest = m
    return oldest


def corpus_sources() -> List[Dict[str, Any]]:
    home = get_home()
    sources: List[Dict[str, Any]] = []

    sources.append({
        "name": "Drive walk (candidate folder tree)",
        "path": "lark/drive_folders.json",
        "mtime": _mtime(os.path.join(home, "lark", "drive_folders.json")),
        "extra": "",
    })

    cfi_p = os.path.join(home, "lark", "candidate_folder_index.json")
    cfi_mtime, cfi_extra = _mtime(cfi_p), ""
    try:
        if os.path.exists(cfi_p):
            with open(cfi_p) as f:
                data = json.load(f)
            if isinstance(data, dict):
                cands = data.get("candidates", {})
                cfi_extra = f"{len(cands)} candidates"
                if data.get("built_at"):
                    cfi_mtime = _to_epoch(data["built_at"]) or cfi_mtime
    except Exception:
        pass
    sources.append({
        "name": "Candidate-folder index",
        "path": "lark/candidate_folder_index.json",
        "mtime": cfi_mtime, "extra": cfi_extra,
    })

    sources.append({
        "name": "Wiki walk (oldest cached page)",
        "path": "lark/docs/wiki/",
        "mtime": _wiki_oldest(os.path.join(home, "lark", "docs", "wiki")),
        "extra": "",
    })

    sub_db = os.path.join(home, "indexes", "submissions.db")
    sub_extra = ""
    try:
        if os.path.exists(sub_db):
            db = sqlite3.connect(sub_db)
            n = db.execute("SELECT COUNT(*) FROM submissions").fetchone()[0]
            db.close()
            sub_extra = f"{n} submissions"
    except Exception:
        pass
    sources.append({
        "name": "Submission indexer",
        "path": "indexes/submissions.db",
        "mtime": _max_synced(sub_db, "submissions"), "extra": sub_extra,
    })

    firm_db = os.path.join(home, "indexes", "firms.db")
    firm_extra = ""
    try:
        if os.path.exists(firm_db):
            db = sqlite3.connect(firm_db)
            fc = db.execute("SELECT COUNT(*) FROM firm_practice").fetchone()[0]
            ff = db.execute("SELECT COUNT(*) FROM firms").fetchone()[0]
            db.close()
            firm_extra = f"{ff} firms · {fc} practice cells"
    except Exception:
        pass
    sources.append({
        "name": "Firm-quality grid",
        "path": "indexes/firms.db",
        "mtime": _max_synced(firm_db, "firm_practice"), "extra": firm_extra,
    })

    sources.append({
        "name": "Last nightly resync",
        "path": "lark/resync.log",
        "mtime": _mtime(os.path.join(home, "lark", "resync.log")),
        "extra": "",
    })

    docs_db = os.path.join(home, "indexes", "company-docs.db")
    chunks, arts = _doc_index_counts(docs_db)
    sources.append({
        "name": "Doc-index (FTS)",
        "path": "indexes/company-docs.db",
        "mtime": _mtime(docs_db),
        "extra": (f"{arts} artifacts · {chunks} chunks"
                  if chunks is not None and arts is not None else ""),
    })

    # Sort by age, OLDEST first — that's the bottleneck.
    sources.sort(key=lambda s: s["mtime"] or 0)
    return sources


def section_corpus(state: State) -> str:
    sources = corpus_sources()
    rows = []
    for i, s in enumerate(sources):
        klass = _fresh_class(s["mtime"])
        # First row is the bottleneck — highlight it.
        bottleneck = ' bottleneck' if i == 0 else ''
        rows.append(
            f'<tr class="{klass}{bottleneck}">'
            f'<td><span class="dot {klass}"></span>{_esc(s["name"])}</td>'
            f'<td class="muted mono small">{_esc(s["path"])}</td>'
            f'<td>{_esc(_fmt_local(s["mtime"]))}</td>'
            f'<td class="muted">{_esc(_ago(s["mtime"]))}</td>'
            f'<td class="muted">{_esc(s["extra"])}</td>'
            '</tr>'
        )
    note = ('<p class="hint">Oldest source first — that’s the bottleneck. '
            'Anything red is over a week stale and probably blocking '
            'something downstream.</p>')
    return _card(
        "Corpus freshness · sorted by staleness",
        f'<table class="data"><thead><tr>'
        f'<th>Source</th><th>Path</th><th>Last refreshed</th>'
        f'<th>Age</th><th>Details</th></tr></thead><tbody>'
        f'{"".join(rows)}</tbody></table>{note}'
    )


# ---------------------------------------------------------------------------
# Section: OAuth / token freshness
# ---------------------------------------------------------------------------

def oauth_status() -> Dict[str, Any]:
    try:
        import lark_oauth
        if hasattr(lark_oauth, "status"):
            return lark_oauth.status() or {}
    except Exception:
        pass
    return {}


def section_oauth(state: State, oauth: Dict[str, Any]) -> str:
    home = get_home()
    keepalive = os.path.join(home, "lark", "oauth-keepalive.log")
    alert = os.path.join(home, "lark", "oauth_alert.txt")

    access_s = oauth.get("access_expires_in_s")
    refresh_d = oauth.get("refresh_expires_in_days")

    def access_str() -> str:
        if access_s is None:
            return "—"
        if access_s <= 0:
            return (f'<span class="stale-red">expired '
                    f'({int(-access_s // 60)} min ago)</span>')
        if access_s < 3600:
            return f"{int(access_s // 60)} min"
        if access_s < 86400:
            return f"{access_s / 3600:.1f} h"
        return f"{access_s / 86400:.1f} d"

    def refresh_str() -> str:
        if refresh_d is None:
            return "—"
        if refresh_d <= 0:
            return '<span class="stale-red">expired</span>'
        klass = "stale-amber" if refresh_d < 3 else ""
        return f'<span class="{klass}">{refresh_d:.1f} d</span>'

    def _b(b):
        if b is None:
            return "—"
        return ('<span class="stale-green">yes</span>' if b
                else '<span class="stale-red">no</span>')

    banner = ""
    if os.path.exists(alert):
        msg = _read_text(alert, 4_000).strip()[:600] or "(empty)"
        banner = (
            '<div class="alert">⚠ OAuth alert flag present — '
            f'<code>lark/oauth_alert.txt</code>:<br>'
            f'<pre>{_esc(msg)}</pre></div>'
        )
    if access_s is not None and access_s <= 0 and not os.path.exists(alert):
        banner += (
            '<div class="alert">⚠ Access token expired and no alert flag '
            'present. The keepalive job likely missed its run — check '
            '<code>lark/oauth-keepalive.log</code>.</div>'
        )

    rows = [
        ("Authorized", _b(oauth.get("authorized"))),
        ("Access valid", _b(oauth.get("access_token_valid"))),
        ("Access expires in", access_str()),
        ("Refresh valid", _b(oauth.get("refresh_valid"))),
        ("Refresh expires in", refresh_str()),
        ("Keep-alive log",
         f'{_esc(_fmt_local(_mtime(keepalive)))} '
         f'<span class="muted">({_esc(_ago(_mtime(keepalive)))})</span>'),
    ]
    body = "".join(f'<tr><th>{k}</th><td>{v}</td></tr>' for k, v in rows)
    return _card("OAuth / token freshness",
                 banner + f'<table class="kv">{body}</table>')


# ---------------------------------------------------------------------------
# Section: bot health (single inline row, not its own card)
# ---------------------------------------------------------------------------

def section_bot_strip() -> str:
    pid, etime = _bot_pid_uptime()
    branch, commit = _git_branch_commit()
    stats = _bot_stats()
    msgs = stats.get("messages_handled", "—")
    port = stats.get("port", "—")
    pid_html = (f'<span class="strong">{_esc(pid)}</span> · up {_esc(etime)}'
                if pid else '<span class="stale-red">not running</span>')
    return (
        '<div class="bot-strip muted small">'
        f'<span>bot {pid_html}</span>'
        f'<span>· {_esc(branch)} @ {_esc(commit)}</span>'
        f'<span>· :{_esc(port)}</span>'
        f'<span>· {_esc(msgs)} msgs since restart</span>'
        '</div>'
    )


# ---------------------------------------------------------------------------
# Section: user memory (per-user persistent context)
# ---------------------------------------------------------------------------
# Two modes:
#   - All users: table — name, fact count, last write, preview.
#   - Filtered (?user=ou_xxx): full listing of every fact file for that
#     user + the recent write-log tail. Operator audit surface.
#
# Read-only; deletes happen via CLI: `python tools/user_memory.py
# delete <open_id> <slug>`.

def section_user_memory(state: State,
                             users: List[Dict[str, Any]]) -> str:
    try:
        from user_memory import (
            all_users_with_memory, summary as rm_summary,
            list_facts as rm_list, tail_write_log as rm_log,
        )
    except Exception as e:
        return _card("User memory",
                     f'<p class="muted">store unavailable: {_esc(e)}</p>')

    name_by_id = {u["open_id"]: _display(u["open_id"],
                                          u.get("display_name"))
                  for u in users}

    # Filtered: per-user detail
    if state.user:
        facts = rm_list(state.user)
        if not facts:
            return _card(
                f"User memory · {_esc(name_by_id.get(state.user, '?'))}",
                '<p class="empty">No persistent facts learned for this '
                'user yet. The bot writes facts only from DM '
                'exchanges — group activity never reaches this store.</p>',
            )
        cards = []
        for f in facts:
            md = f.get("metadata") or {}
            slug = _esc(f.get("slug", ""))
            ftype = _esc(md.get("type", ""))
            updated = _esc(_fmt_local(md.get("updated_at", "")))
            ago = _esc(_ago(md.get("updated_at", "")))
            rc = _esc(md.get("reinforcement_count", 1))
            cards.append(
                '<div class="fact-card">'
                f'<div class="fact-head">'
                f'<span class="fact-slug mono">{slug}</span>'
                f'<span class="chip-i">{ftype}</span>'
                f'<span class="muted small">'
                f'updated {updated} ({ago}) · ×{rc}</span>'
                f'</div>'
                f'<div class="fact-desc">{_esc(f.get("description",""))}</div>'
                f'<div class="fact-body">'
                f'{_markdown_lite(f.get("body","") or "")}</div>'
                '</div>'
            )
        log = rm_log(state.user, n=20)
        log_lines = "".join(
            '<tr>'
            f'<td class="muted small mono">{_esc(_fmt_local(r.get("ts","")))}</td>'
            f'<td class="mono small">{_esc(r.get("action","?"))}</td>'
            f'<td class="mono small">{_esc(r.get("slug","?"))}</td>'
            f'<td class="muted small">{_esc(r.get("trigger_workflow",""))}</td>'
            '</tr>'
            for r in reversed(log)
        ) or '<tr><td colspan="4" class="muted">no writes logged</td></tr>'
        return _card(
            f"User memory · {_esc(name_by_id.get(state.user, '?'))}"
            f" · {len(facts)} fact(s)",
            "".join(cards)
            + '<h4 class="mt">Write log · last 20</h4>'
            + '<table class="data"><thead><tr>'
            '<th>When</th><th>Action</th><th>Slug</th><th>Workflow</th>'
            '</tr></thead><tbody>'
            + log_lines
            + '</tbody></table>'
            + '<p class="hint">Edit/delete a fact via CLI: '
            '<code>python tools/user_memory.py delete '
            f'{_esc(state.user)} &lt;slug&gt;</code></p>'
        )

    # Overview: one row per user with any memory
    rows = []
    for open_id in all_users_with_memory():
        s = rm_summary(open_id)
        if s["fact_count"] == 0:
            continue
        name = _display(open_id, name_by_id.get(open_id))
        preview = ", ".join(
            f'<span class="fact-slug mono">{_esc(p["slug"])}</span>'
            for p in s["preview"][:3]
        ) or '<span class="muted">—</span>'
        rows.append(
            '<tr>'
            f'<td class="name"><a href="{_esc(state.url(user=open_id))}">'
            f'{_esc(name)}</a>'
            f'<div class="oid">{_esc(open_id)}</div></td>'
            f'<td class="num strong">{_esc(s["fact_count"])}</td>'
            f'<td class="muted small">{_esc(_ago(s["last_write_ts"]))}</td>'
            f'<td class="small">{preview}</td>'
            '</tr>'
        )

    if not rows:
        return _card(
            "User memory",
            '<p class="empty">No user memory yet. The bot writes '
            'facts only from DM exchanges with each user; nothing '
            'has been learned so far. Filter to a user (or have '
            'one DM the bot) to populate this section.</p>',
        )

    return _card(
        f"User memory · {len(rows)} user(s) with memory",
        '<table class="data"><thead><tr>'
        '<th>User</th>'
        '<th class="num">Facts</th>'
        '<th>Last write</th>'
        '<th>Top facts</th>'
        '</tr></thead><tbody>'
        + "".join(rows)
        + '</tbody></table>'
        '<p class="hint">Click a user to see every fact + recent '
        'write log. Memory is DM-only; group chats never touch it.</p>',
    )


# ---------------------------------------------------------------------------
# Section: roadmap (engineering backlog + Eisenhower)
# ---------------------------------------------------------------------------

def section_roadmap() -> str:
    home = get_home()
    eng_p = os.path.join(home, "brain", "engineering-backlog.md")
    eis_p = os.path.join(home, "brain", "eisenhower.md")
    eng_text = _read_text(eng_p)
    eis_text = _read_text(eis_p)
    eng_open = sum(1 for ln in eng_text.splitlines()
                   if ln.lstrip().startswith("- [ ]"))
    eng_done = sum(1 for ln in eng_text.splitlines()
                   if ln.lstrip().startswith("- [x]"))
    return _card(
        "Roadmap",
        '<div class="split">'
        '<div class="split-l">'
        f'<h4>Engineering backlog · {eng_open} open · {eng_done} done</h4>'
        f'<div class="muted small">{_esc(_fmt_local(_mtime(eng_p)))} '
        f'· {_esc(_ago(_mtime(eng_p)))}</div>'
        f'<div class="scroll tall">{_markdown_lite(eng_text)}</div>'
        '</div>'
        '<div class="split-r">'
        '<h4>Eisenhower (operational)</h4>'
        f'<div class="muted small">{_esc(_fmt_local(_mtime(eis_p)))} '
        f'· {_esc(_ago(_mtime(eis_p)))}</div>'
        f'<div class="scroll tall">{_markdown_lite(eis_text)}</div>'
        '</div></div>'
    )


# ---------------------------------------------------------------------------
# Card chrome
# ---------------------------------------------------------------------------

def _card(title: str, body: str) -> str:
    return (
        '<section class="card">'
        f'<h2>{_esc(title)}</h2>'
        f'<div class="card-body">{body}</div>'
        '</section>'
    )


# ---------------------------------------------------------------------------
# Stylesheet — applies impeccable's shared design laws + product register
#   - OKLCH, tinted neutrals (hue 260), restrained accent at 0.52 / 0.18
#   - light primary, dark via prefers-color-scheme
#   - system font stack, tabular numerals for data, monospace for ids
#   - generous outer spacing, tight inner (data density done right)
# ---------------------------------------------------------------------------

_STYLE = r"""
:root {
  --bg:        oklch(0.985 0.005 260);
  --surface:   oklch(1.000 0.000 0);
  --raised:    oklch(0.975 0.005 260);
  --border:    oklch(0.92  0.008 260);
  --border-2:  oklch(0.86  0.012 260);
  --text:      oklch(0.20  0.020 260);
  --muted:     oklch(0.50  0.012 260);
  --hint:      oklch(0.40  0.010 260);
  --accent:    oklch(0.52  0.18  268);
  --accent-2:  oklch(0.95  0.04  268);
  --green:     oklch(0.62  0.13  150);
  --green-bg:  oklch(0.96  0.04  150);
  --amber:     oklch(0.70  0.13  70);
  --amber-bg:  oklch(0.97  0.05  80);
  --red:       oklch(0.58  0.20  25);
  --red-bg:    oklch(0.96  0.05  25);
  --shadow:    0 1px 0 oklch(0.90 0.008 260),
               0 1px 3px oklch(0.90 0.008 260 / 0.3);
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg:        oklch(0.16  0.012 260);
    --surface:   oklch(0.20  0.012 260);
    --raised:    oklch(0.23  0.012 260);
    --border:    oklch(0.28  0.014 260);
    --border-2:  oklch(0.36  0.016 260);
    --text:      oklch(0.94  0.010 260);
    --muted:     oklch(0.62  0.014 260);
    --hint:      oklch(0.74  0.012 260);
    --accent:    oklch(0.72  0.18  268);
    --accent-2:  oklch(0.30  0.08  268);
    --green-bg:  oklch(0.28  0.06  150);
    --amber-bg:  oklch(0.30  0.07  80);
    --red-bg:    oklch(0.30  0.10  25);
    --shadow:    0 1px 0 oklch(0.12 0.008 260),
                 0 1px 3px oklch(0.10 0.008 260 / 0.5);
  }
}

* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; background: var(--bg); color: var(--text); }
body {
  font: 13.5px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI",
        system-ui, Roboto, "Helvetica Neue", Arial, sans-serif;
  font-variant-numeric: tabular-nums;
  padding: 0 0 64px;
  min-height: 100vh;
}
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
.wrap { max-width: 1200px; margin: 0 auto; padding: 0 20px; }
.muted { color: var(--muted); }
.small { font-size: 12px; }
.mono  { font-family: ui-monospace, SF Mono, Menlo, Consolas, monospace; }
.strong { font-weight: 600; }
.num    { text-align: right; }
.empty  { color: var(--muted); padding: 14px 0; }
.hint   { color: var(--hint); font-size: 12px; margin: 8px 0 0; }

/* ── Top filter bar — sticky, full-width tint */
.filter-bar {
  position: sticky; top: 0; z-index: 10;
  background: var(--surface);
  border-bottom: 1px solid var(--border);
  padding: 16px 20px 14px;
  display: grid;
  grid-template-columns: auto auto 1fr;
  align-items: center;
  gap: 12px 20px;
  margin-bottom: 20px;
}
@media (max-width: 800px) {
  .filter-bar { grid-template-columns: 1fr; }
  .filter-others { display: none; }
}
.filter-current { font-size: 15px; font-weight: 600; }
.chip-active {
  display: inline-flex; align-items: center; gap: 8px;
  padding: 4px 6px 4px 12px;
  background: var(--accent); color: white;
  border-radius: 999px;
  font-weight: 600; font-size: 13px;
  text-decoration: none;
}
.chip-active:hover { text-decoration: none; opacity: 0.92; }
.chip-close {
  display: inline-flex; width: 18px; height: 18px;
  background: rgba(255,255,255,0.25);
  border-radius: 50%;
  align-items: center; justify-content: center;
  font-size: 14px; line-height: 1;
}
.chip-all {
  display: inline-block;
  padding: 4px 12px;
  background: var(--raised); color: var(--text);
  border-radius: 999px; font-weight: 600; font-size: 13px;
}
.filter-sub { color: var(--muted); font-size: 12px; }
.filter-others { color: var(--muted); font-size: 13px; text-align: right; }
.filter-others a { color: var(--accent); }

/* ── Bot strip (one line under the filter bar) */
.bot-strip {
  display: flex; gap: 14px; flex-wrap: wrap;
  padding: 14px 0 14px;
  border-bottom: 1px solid var(--border);
  margin-bottom: 24px;
  color: var(--hint);
}
.bot-strip .strong { color: var(--text); }

/* ── Hero KPIs — 3-column at the widths Alejandro actually uses
   (laptop + funnel URL viewport), 6-column only at very wide. The
   6-up makes Top-workflow + Oldest-source labels wrap mid-word. */
.hero {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 12px;
  margin: 0 0 24px;
}
@media (min-width: 1400px) { .hero { grid-template-columns: repeat(6, 1fr); } }
@media (max-width: 560px)  { .hero { grid-template-columns: repeat(2, 1fr); } }
.tile {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 14px 16px 12px;
  min-height: 96px;
}
.tile-label {
  font-size: 11px; font-weight: 600;
  text-transform: uppercase; letter-spacing: 0.06em;
  color: var(--muted);
  margin-bottom: 6px;
}
.tile-value {
  font-size: 28px; font-weight: 600; letter-spacing: -0.01em;
  line-height: 1.15;
  word-break: break-word;
}
.tile-sub  { font-size: 12px; color: var(--muted); margin-top: 4px; }
.tile.stale-green .tile-value { color: var(--green); }
.tile.stale-amber .tile-value { color: var(--amber); }
.tile.stale-red   .tile-value { color: var(--red); }

/* ── Cards */
.card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 18px 20px;
  margin: 0 0 18px;
}
.card h2 {
  margin: 0 0 14px;
  font-size: 11px; letter-spacing: 0.08em;
  text-transform: uppercase;
  font-weight: 600; color: var(--muted);
}
.card h4 {
  margin: 16px 0 8px;
  font-size: 12px; letter-spacing: 0.04em;
  text-transform: uppercase;
  font-weight: 600; color: var(--muted);
}

/* ── Tables */
table { width: 100%; border-collapse: collapse; }
table.kv th {
  text-align: left; width: 200px;
  color: var(--muted); font-weight: 500;
  padding: 6px 0; vertical-align: top;
}
table.kv td { padding: 6px 0; }
table.data thead th {
  text-align: left;
  padding: 9px 12px;
  border-bottom: 1px solid var(--border-2);
  color: var(--muted);
  font-weight: 600; font-size: 12px;
}
table.data thead th.num { text-align: right; }
table.data thead th a { color: inherit; }
table.data thead th.sorted { color: var(--accent); }
.arrow { color: var(--accent); }
table.data tbody td {
  padding: 9px 12px;
  border-bottom: 1px solid var(--border);
  vertical-align: top;
}
table.data tbody tr:last-child td { border-bottom: none; }
table.data tbody tr:hover { background: var(--raised); }
table.data tbody tr.active { background: var(--accent-2); }
td.name a { font-weight: 600; color: var(--text); }
td.name a:hover { color: var(--accent); text-decoration: none; }
.oid { font: 11px/1.2 ui-monospace, SF Mono, Menlo, monospace;
       color: var(--muted); margin-top: 2px; }

/* ── Staleness rows + dots */
.dot {
  display: inline-block; width: 8px; height: 8px;
  border-radius: 50%; margin-right: 9px; vertical-align: middle;
}
.stale-green   { color: var(--green); }
.stale-amber   { color: var(--amber); }
.stale-red     { color: var(--red); }
.dot.stale-green { background: var(--green); }
.dot.stale-amber { background: var(--amber); }
.dot.stale-red   { background: var(--red); }
tr.stale-amber { background: var(--amber-bg); }
tr.stale-red   { background: var(--red-bg); }
tr.bottleneck td:first-child::before {
  content: "BOTTLENECK"; display: inline-block;
  background: var(--red); color: white;
  font-size: 10px; font-weight: 700; letter-spacing: 0.04em;
  padding: 1px 6px; border-radius: 3px;
  margin-right: 8px; vertical-align: 2px;
}

/* ── Split layout (workflows | tokens, feedback halves, etc.) */
.split { display: grid; grid-template-columns: 1fr 1fr; gap: 28px; }
@media (max-width: 900px) { .split { grid-template-columns: 1fr; } }

/* ── Sparkline */
.spark { padding: 12px 0 6px; }
.spark-svg { display: block; max-width: 100%; height: auto; }
.spark-line { fill: none; stroke: var(--accent); stroke-width: 1.5;
              stroke-linejoin: round; }
.spark-fill { fill: var(--accent); opacity: 0.10; stroke: none; }
.spark-label { font: 11px ui-monospace, SF Mono, monospace;
               fill: var(--muted); }

/* ── Chips (for feedback breakdown) */
.chips { display: flex; gap: 6px; flex-wrap: wrap;
         margin: 4px 0 14px; }
.chip {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 3px 4px 3px 10px;
  background: var(--raised); border-radius: 999px;
  font-size: 12px;
}
.chip b { display: inline-block;
          background: var(--accent); color: white;
          padding: 1px 8px; border-radius: 999px;
          font-weight: 600; font-size: 12px; }
.chip-i {
  display: inline-block; padding: 1px 7px;
  background: var(--raised); color: var(--muted);
  border-radius: 4px; font-size: 11px;
}

/* ── Feedback list */
ul.fb { list-style: none; padding: 0; margin: 4px 0; }
ul.fb li { padding: 10px 0;
           border-bottom: 1px solid var(--border); }
ul.fb li:last-child { border-bottom: none; }
.who { font-weight: 600; }
.fb-text { margin: 4px 0; }

/* ── User memory — per-fact cards in the filtered view */
.fact-card {
  background: var(--raised); border: 1px solid var(--border);
  border-radius: 8px; padding: 12px 14px; margin: 0 0 12px;
}
.fact-head {
  display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
  margin-bottom: 4px;
}
.fact-slug { font-weight: 600; }
.fact-desc { color: var(--muted); font-size: 12px; margin-bottom: 6px; }
.fact-body p { margin: 4px 0; font-size: 13px; }
.fact-body h3, .fact-body h4 { margin: 6px 0 4px; font-size: 13px; }
.fact-body ul { margin: 4px 0 8px 18px; padding: 0; }

/* ── OAuth banner */
.alert {
  background: var(--red-bg);
  border-left: 3px solid var(--red);
  color: var(--text);
  padding: 10px 14px; border-radius: 6px;
  margin-bottom: 10px;
}
.alert pre {
  margin: 6px 0 0; white-space: pre-wrap;
  font: 12px/1.4 ui-monospace, SF Mono, monospace;
}

/* ── Scrollable preview boxes (lessons, roadmap markdown) */
.scroll {
  max-height: 240px; overflow-y: auto;
  padding: 10px 14px;
  background: var(--raised);
  border: 1px solid var(--border);
  border-radius: 6px;
  font-size: 13px;
}
.scroll.tall { max-height: 380px; }
.scroll h3, .scroll h4 { margin: 8px 0 4px; color: var(--text); }
.scroll ul { margin: 4px 0 8px 18px; padding: 0; }
.scroll p  { margin: 4px 0; }

/* ── Inline code chips */
code, code.path {
  background: var(--raised);
  padding: 1px 6px; border-radius: 4px;
  font: 12px ui-monospace, SF Mono, Menlo, monospace;
}
"""


# ---------------------------------------------------------------------------
# Composer
# ---------------------------------------------------------------------------

def render_dashboard(filter_user: Optional[str] = None,
                     sort: Optional[str] = None,
                     key_for_links: str = "") -> str:
    """Compose the full HTML page. Pure render, no side effects."""
    from usage_store import UsageStore
    state = State(key=key_for_links, user=filter_user, sort=sort)
    s = UsageStore.get()

    # Pull primitives once so renderers can lay out without re-querying
    users = s.users_summary(window_days=WINDOW_DAYS)
    active_user = next((u for u in users if u["open_id"] == state.user), None)

    # Apply requested sort (Python-side — simpler than re-querying)
    def _tok_total(u):
        return (int(u.get("input_tokens") or 0)
                + int(u.get("output_tokens") or 0)
                + int(u.get("cache_read") or 0)
                + int(u.get("cache_creation") or 0))
    sort_fns = {
        "msgs":   lambda u: -int(u.get("msgs_window") or 0),
        "msgs30": lambda u: -int(u.get("msgs_30d") or 0),
        "tokens": lambda u: -_tok_total(u),
        "cost":   lambda u: -float(u.get("cost_usd") or 0),
        "last":   lambda u: -(u.get("last_seen") or 0),
        "name":   lambda u: (u.get("display_name") or "").lower() or u["open_id"],
    }
    users = sorted(users, key=sort_fns.get(state.sort, sort_fns["msgs"]))

    kpis = s.headline_kpis(window_days=WINDOW_DAYS)
    wf = s.workflows_breakdown(window_days=WINDOW_DAYS,
                                user_open_id=state.user)
    daily = s.messages_by_day(days=14, user_open_id=state.user)
    # corpus_sources is already sorted oldest-first; the bottleneck
    # is sources[0] (shown in the corpus table with a red tag) and the
    # most-recent refresh is sources[-1] (shown in the hero tile).
    sources = corpus_sources()
    with_mtime = [s for s in sources if s.get("mtime")]
    oldest = ((with_mtime[0]["name"], with_mtime[0]["mtime"])
              if with_mtime else None)
    newest = ((with_mtime[-1]["name"], with_mtime[-1]["mtime"])
              if with_mtime else None)
    oauth = oauth_status()

    body = "".join([
        section_filter_bar(state, users, active_user),
        '<div class="wrap">',
        section_bot_strip(),
        section_hero(state, kpis, oldest, newest, oauth),
        section_users(state, users),
        section_workflows(state, wf),
        section_tokens(state, daily, wf),
        section_feedback(state),
        section_user_memory(state, users),
        section_corpus(state),
        section_oauth(state, oauth),
        section_roadmap(),
        '</div>',
    ])
    return (
        '<!doctype html>'
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta http-equiv="refresh" content="60">'
        '<title>Noto — Operator</title>'
        f'<style>{_STYLE}</style>'
        '</head><body>' + body + '</body></html>'
    )


# ---------------------------------------------------------------------------
# Auth helper (read by lark_bot's /dashboard route)
# ---------------------------------------------------------------------------

def dashboard_key() -> str:
    try:
        import yaml
        cred_path = get_path("credentials")
        if not os.path.exists(cred_path):
            return ""
        with open(cred_path) as f:
            data = yaml.safe_load(f) or {}
        return str((data.get("dashboard") or {}).get("key", "") or "")
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", help="Filter to a specific user open_id")
    ap.add_argument("--sort", help=f"Sort key — one of {list(_SORT_KEYS)}")
    args = ap.parse_args()
    sys.stdout.write(render_dashboard(filter_user=args.user, sort=args.sort))
