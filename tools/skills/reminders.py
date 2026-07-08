"""
Reminders skill — per-user task lists backed by Lark Tasks (task v2).

"noto remind me to chase the Skadden partner tomorrow 3pm" becomes a
task in the user's personal "Noto — <Name>" tasklist (created on
first use, shared with them as editor), assigned to them, with a Lark
alert at the due time. Undated/all-day reminders surface in the
per-user morning digest (tools/reminders_digest.py) instead.

Date words are resolved downstream of the planner (same pattern as
screenshot_calendar): the planner passes the request text verbatim and
_extract() resolves "tomorrow"/"friday" against TODAY in the
requester's own timezone. Timezone priority for timed reminders reuses
screenshot_calendar.event_tz (named in request > travel > home tz).

Feature-flagged: h2.reminders_enabled (notolark.yaml).
"""

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _enabled() -> bool:
    try:
        from config import load_config
        return bool((load_config().get("h2") or {})
                    .get("reminders_enabled", False))
    except Exception:
        return False


_DISABLED = {"ok": False, "error": "reminders_disabled"}


def _person(sender_open_id: str) -> Dict[str, Any]:
    import lark_bot as lb
    return lb._resolve_operator(sender_open_id) or {}


def _home_tz(sender_open_id: str) -> str:
    tz = (_person(sender_open_id).get("timezone") or "").strip()
    if tz:
        return tz
    try:
        from config import load_config
        return (load_config().get("system", {})
                .get("timezone", "Asia/Singapore"))
    except Exception:
        return "Asia/Singapore"


_EXTRACT_PROMPT = """A user is asking their assistant to remember \
or remind them of something. TODAY is {today} in their timezone \
({tz}).

THEIR MESSAGE:
```
{text}
```

Extract the reminder. Reply with ONLY a JSON object:
{{"is_reminder": <true if this asks to remember/remind/track a to-do>,
 "summary": "<the to-do itself, imperative, without the remind-me
wrapper: 'Call Garrett about the HK offer', or null>",
 "date": "<YYYY-MM-DD, resolving today/tomorrow/weekday words against
TODAY; null if no day given>",
 "time": "<HH:MM 24h if a clock time was given, else null>",
 "timezone": "<IANA zone like America/New_York IF named or implied
(ET/PT/HKT/'3pm Eastern'…), else null>"}}
"""


def _extract(text: str, tz_name: str) -> Optional[Dict[str, Any]]:
    from noto_research import _claude
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo(tz_name))
    except Exception:
        now = datetime.now()
    today = now.strftime("%Y-%m-%d (%A, %H:%M)")
    raw = _claude(_EXTRACT_PROMPT.format(
        today=today, tz=tz_name, text=(text or "")[:1500]),
        timeout=60, web=False) or ""
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return None
    try:
        out = json.loads(m.group(0))
        return out if isinstance(out, dict) else None
    except Exception:
        return None


def _due_from(ev: Dict[str, Any], sender_open_id: str
              ) -> Dict[str, Any]:
    """Resolve extracted date/time into {due_ts_ms, is_all_day,
    tz_name, display}. No date and no time → no due at all (per
    operator spec: only alert when they named a time)."""
    from zoneinfo import ZoneInfo
    date_s = (ev.get("date") or "").strip() or None
    time_s = (ev.get("time") or "").strip() or None
    if not date_s and not time_s:
        return {"due_ts_ms": None, "is_all_day": False,
                "tz_name": "", "display": ""}
    # tz priority: named in request > travel > home (calendar rule)
    import screenshot_calendar as sc
    tz_name = sc.event_tz(sender_open_id,
                          {"timezone": ev.get("timezone"),
                           "date": date_s}) or _home_tz(sender_open_id)
    tz = ZoneInfo(tz_name)
    if time_s and not date_s:
        # "remind me at 5pm" — today, or tomorrow if already past
        now = datetime.now(tz)
        hh, mm = time_s.split(":")
        cand = now.replace(hour=int(hh), minute=int(mm),
                           second=0, microsecond=0)
        if cand <= now:
            cand += timedelta(days=1)
        return {"due_ts_ms": int(cand.timestamp() * 1000),
                "is_all_day": False, "tz_name": tz_name,
                "display": cand.strftime("%a %b %d, %H:%M ") + tz_name}
    if date_s and time_s:
        local = datetime.strptime(f"{date_s} {time_s}",
                                  "%Y-%m-%d %H:%M").replace(tzinfo=tz)
        return {"due_ts_ms": int(local.timestamp() * 1000),
                "is_all_day": False, "tz_name": tz_name,
                "display": local.strftime("%a %b %d, %H:%M ") + tz_name}
    # date only → all-day due. Lark expects UTC-midnight ms for
    # is_all_day tasks (not local midnight).
    d = datetime.strptime(date_s, "%Y-%m-%d").replace(
        tzinfo=timezone.utc)
    return {"due_ts_ms": int(d.timestamp() * 1000),
            "is_all_day": True, "tz_name": tz_name,
            "display": d.strftime("%a %b %d (all day)")}


def add_reminder(sender_open_id: str, text: str,
                 on_progress: Optional[Callable[[str], None]] = None
                 ) -> Dict[str, Any]:
    """Create a reminder from the verbatim request text. Returns
    {ok, summary, due_display, alert, task_guid, tasklist_name} or
    {ok: False, error}."""
    if not _enabled():
        return dict(_DISABLED)
    if on_progress:
        on_progress("⏰ Setting up your reminder…")
    ev = _extract(text, _home_tz(sender_open_id))
    if not ev or not ev.get("is_reminder") or not ev.get("summary"):
        return {"ok": False, "error": "not_a_reminder",
                "detail": "couldn't find a to-do in the message"}
    try:
        due = _due_from(ev, sender_open_id)
    except Exception as e:
        return {"ok": False, "error": f"bad date/time: {e}"}
    person = _person(sender_open_id)
    name = person.get("name") or ""
    if not name:
        import lark_bot as lb
        name = lb._display_name(sender_open_id)
    try:
        import lark_tasks as lt
        task = lt.create_task(sender_open_id, ev["summary"],
                              display_name=name,
                              due_ts_ms=due["due_ts_ms"],
                              is_all_day=due["is_all_day"])
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True,
            "summary": ev["summary"],
            "due_display": due["display"],
            "alert": bool(due["due_ts_ms"] and not due["is_all_day"]),
            "task_guid": task.get("guid", ""),
            "tasklist_name": f"Noto — {name}"}


def _open_items(sender_open_id: str) -> List[Dict[str, Any]]:
    import lark_tasks as lt
    guid = lt.user_tasklist_guid(sender_open_id)
    if not guid:
        return []
    return lt.list_tasks(guid, completed=False)


def _fmt_due(task: Dict[str, Any], tz_name: str) -> str:
    due = task.get("due") or {}
    ts = due.get("timestamp")
    if not ts:
        return ""
    from zoneinfo import ZoneInfo
    dt = datetime.fromtimestamp(int(ts) / 1000, ZoneInfo(tz_name))
    if due.get("is_all_day"):
        # stored as UTC midnight — render the date without tz shift
        return datetime.fromtimestamp(
            int(ts) / 1000, timezone.utc).strftime("%a %b %d")
    return dt.strftime("%a %b %d, %H:%M")


def list_reminders(sender_open_id: str) -> Dict[str, Any]:
    """Open reminders for the sender, dated ones first. Returns
    {ok, items: [{guid, summary, due_display, overdue}], count}."""
    if not _enabled():
        return dict(_DISABLED)
    try:
        items = _open_items(sender_open_id)
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}
    tz_name = _home_tz(sender_open_id)
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    out = []
    for t in items:
        due = t.get("due") or {}
        ts = int(due.get("timestamp") or 0)
        out.append({
            "guid": t.get("guid", ""),
            "summary": t.get("summary", ""),
            "due_ts_ms": ts,
            "due_display": _fmt_due(t, tz_name),
            "overdue": bool(ts and not due.get("is_all_day")
                            and ts < now_ms),
        })
    out.sort(key=lambda x: (x["due_ts_ms"] == 0, x["due_ts_ms"]))
    return {"ok": True, "items": out, "count": len(out)}


def complete_reminder(sender_open_id: str, query: str
                      ) -> Dict[str, Any]:
    """Mark the reminder matching `query` as done. Single unambiguous
    match → completes it. Otherwise returns the candidates so the bot
    can ask which one."""
    if not _enabled():
        return dict(_DISABLED)
    q = (query or "").casefold().strip()
    if not q:
        return {"ok": False, "error": "empty query"}
    try:
        items = _open_items(sender_open_id)
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}
    if not items:
        return {"ok": False, "error": "no_open_reminders"}

    def _score(t: Dict[str, Any]) -> int:
        s = (t.get("summary") or "").casefold()
        if q == s:
            return 3
        if q in s or s in q:
            return 2
        qw = set(q.split())
        return 1 if qw and qw <= set(s.split()) else 0

    scored = [(t, _score(t)) for t in items]
    best = max(s for _, s in scored)
    matches = [t for t, s in scored if s == best and s > 0]
    if not matches:
        return {"ok": False, "error": "no_match",
                "items": [t.get("summary", "") for t in items[:10]]}
    if len(matches) > 1:
        return {"ok": False, "error": "ambiguous",
                "items": [t.get("summary", "") for t in matches[:10]]}
    import lark_tasks as lt
    done = lt.complete_task(matches[0]["guid"])
    if (done.get("completed_at") or "0") == "0":
        return {"ok": False, "error": "complete_failed"}
    return {"ok": True, "summary": matches[0].get("summary", "")}
