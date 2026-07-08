"""
Per-user morning reminders digest.

Runs every 15 min via launchd (com.noto.remindersdigest). For each
person in memory/operators.yaml whose LOCAL clock (their timezone) is
past 08:00 and who hasn't been sent today's digest yet, DMs them the
reminders due today + anything overdue from their "Noto — <Name>"
task list. People with nothing pending get no message (their state is
still stamped so we don't re-check all day). Timed reminders also get
Lark's native at-due-time alert — the digest is the morning roundup,
not the alert mechanism.

Idempotent: brain/reminders-digest-state.json maps open_id → last
LOCAL date sent. Feature-flagged: h2.reminders_enabled.

CLI:
  python tools/reminders_digest.py post            # normal 15-min tick
  python tools/reminders_digest.py post --force    # ignore time-of-day + state
  python tools/reminders_digest.py preview         # print, send nothing
"""

import json
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

SEND_FROM_HOUR = 8  # local time; first tick past 08:00 sends


def _home() -> str:
    from config import get_home
    return get_home()


def _enabled() -> bool:
    from config import load_config
    return bool((load_config().get("h2") or {})
                .get("reminders_enabled", False))


def _state_path() -> str:
    return os.path.join(_home(), "brain",
                         "reminders-digest-state.json")


def _load_state() -> Dict[str, Any]:
    try:
        with open(_state_path()) as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _save_state(state: Dict[str, Any]) -> None:
    p = _state_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        json.dump(state, f, indent=2)


def _people() -> List[Dict[str, Any]]:
    """operators.yaml records that are real humans with a timezone."""
    import yaml
    path = os.path.join(_home(), "memory", "operators.yaml")
    try:
        ops = yaml.safe_load(open(path)) or {}
    except Exception:
        return []
    out = []
    for rec in ops.values():
        if not isinstance(rec, dict):
            continue
        if rec.get("role") == "service_account":
            continue
        if rec.get("open_id") and rec.get("timezone"):
            out.append(rec)
    return out


def _bucket(tasks: List[Dict[str, Any]], tz: ZoneInfo
            ) -> Dict[str, List[Dict[str, Any]]]:
    """Split open tasks into overdue / due today / undated-or-later,
    judged on the recipient's local calendar day."""
    now = datetime.now(tz)
    sod = now.replace(hour=0, minute=0, second=0, microsecond=0)
    eod_ms = int((sod.timestamp() + 86400) * 1000)
    sod_ms = int(sod.timestamp() * 1000)
    overdue, today, rest = [], [], []
    for t in tasks:
        due = t.get("due") or {}
        ts = int(due.get("timestamp") or 0)
        if not ts:
            rest.append(t)
        elif due.get("is_all_day"):
            # all-day dues are UTC-midnight stamps — compare dates
            d = datetime.fromtimestamp(ts / 1000, timezone.utc).date()
            if d < now.date():
                overdue.append(t)
            elif d == now.date():
                today.append(t)
            else:
                rest.append(t)
        elif ts < sod_ms:
            overdue.append(t)
        elif ts < eod_ms:
            today.append(t)
        else:
            rest.append(t)
    return {"overdue": overdue, "today": today, "rest": rest}


def _fmt_line(t: Dict[str, Any], tz: ZoneInfo) -> str:
    due = t.get("due") or {}
    ts = int(due.get("timestamp") or 0)
    s = f"  • {(t.get('summary') or '?')[:80]}"
    if ts and not due.get("is_all_day"):
        s += datetime.fromtimestamp(ts / 1000, tz).strftime(
            "  _(%a %H:%M)_")
    elif ts:
        s += datetime.fromtimestamp(ts / 1000, timezone.utc).strftime(
            "  _(%a %b %d)_")
    return s


def _format_digest(name: str, buckets: Dict[str, List[Dict[str, Any]]],
                   tz: ZoneInfo) -> str:
    today = datetime.now(tz).strftime("%a %b %d")
    first = (name or "").split()[0] if name else "there"
    lines = [f"☀️ Morning {first} — your reminders for {today}:"]
    if buckets["overdue"]:
        lines.append(f"\n**⚠️ Overdue ({len(buckets['overdue'])})**")
        lines += [_fmt_line(t, tz) for t in buckets["overdue"][:15]]
    if buckets["today"]:
        lines.append(f"\n**📌 Today ({len(buckets['today'])})**")
        lines += [_fmt_line(t, tz) for t in buckets["today"][:15]]
    if buckets["rest"]:
        lines.append(f"\n_…plus {len(buckets['rest'])} more on your "
                     f"list without a date (say “noto what's on my "
                     f"list” to see everything)._")
    lines.append("\n_Tick items off in Lark Tasks, or tell me "
                 "“done with …”._")
    return "\n".join(lines)


def post(force: bool = False, verbose: bool = True) -> Dict[str, Any]:
    """One tick: send morning digests to everyone whose local morning
    has arrived and who hasn't had today's yet."""
    if not _enabled():
        if verbose:
            print("[reminders_digest] h2.reminders_enabled is off — "
                  "skipping", flush=True)
        return {"sent": 0, "reason": "disabled"}
    import lark_tasks as lt
    state = _load_state()
    sent, checked = 0, 0
    for person in _people():
        oid = person["open_id"]
        try:
            tz = ZoneInfo(person["timezone"])
        except Exception:
            continue
        local = datetime.now(tz)
        local_date = local.strftime("%Y-%m-%d")
        if not force:
            if local.hour < SEND_FROM_HOUR:
                continue
            if state.get(oid) == local_date:
                continue
        guid = lt.user_tasklist_guid(oid)
        if not guid:
            state[oid] = local_date  # nothing to digest for them
            continue
        checked += 1
        try:
            buckets = _bucket(lt.list_tasks(guid, completed=False), tz)
        except RuntimeError as e:
            print(f"[reminders_digest] list failed for "
                  f"{person.get('name','?')}: {str(e)[:160]}",
                  file=sys.stderr, flush=True)
            continue  # retry next tick, don't stamp
        if not (buckets["overdue"] or buckets["today"]):
            state[oid] = local_date  # quiet morning — no DM
            continue
        body = _format_digest(person.get("name", ""), buckets, tz)
        try:
            from lark_client import LarkClient
            LarkClient().send_text(oid, body,
                                   receive_id_type="open_id")
            state[oid] = local_date
            sent += 1
            if verbose:
                print(f"[reminders_digest] sent to "
                      f"{person.get('name','?')} "
                      f"({len(buckets['overdue'])} overdue, "
                      f"{len(buckets['today'])} today)", flush=True)
        except Exception as e:
            print(f"[reminders_digest] DM failed for "
                  f"{person.get('name','?')}: {str(e)[:160]}",
                  file=sys.stderr, flush=True)
    _save_state(state)
    return {"sent": sent, "with_lists": checked}


def preview() -> str:
    """What each person WOULD receive right now (ignores time-of-day
    and sent-state; sends nothing)."""
    import lark_tasks as lt
    out = []
    for person in _people():
        oid = person["open_id"]
        guid = lt.user_tasklist_guid(oid)
        if not guid:
            continue
        tz = ZoneInfo(person["timezone"])
        try:
            buckets = _bucket(lt.list_tasks(guid, completed=False), tz)
        except RuntimeError as e:
            out.append(f"--- {person.get('name','?')}: list failed "
                       f"({str(e)[:120]})")
            continue
        if not (buckets["overdue"] or buckets["today"]):
            out.append(f"--- {person.get('name','?')}: nothing due "
                       f"(digest would be skipped)")
            continue
        out.append(f"--- {person.get('name','?')} "
                   f"[{person['timezone']}]\n"
                   + _format_digest(person.get("name", ""),
                                    buckets, tz))
    return "\n".join(out) if out else "(no registered task lists yet)"


def main(argv: List[str]) -> int:
    if not argv:
        print(__doc__.strip())
        return 0
    cmd = argv[0]
    if cmd == "post":
        print(json.dumps(post(force="--force" in argv), indent=2))
        return 0
    if cmd == "preview":
        print(preview())
        return 0
    print("commands: post [--force] | preview", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
