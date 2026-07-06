"""
Lark Calendar — thin wrapper around /open-apis/calendar/v4.

Uses the Noah service user's OAuth token (calendar:calendar scope is
already approved + live). Built for the email-driven pipeline: when an
email implies "schedule X with Y on date Z", the bot creates a draft
event here after operator approval via the Pipeline Management poll
card.

Lark Calendar primer (so the call shapes make sense):
  - A Lark user has N calendars. Their primary one has role='owner'.
  - An event is created on a specific calendar_id and can include
    attendees (other Lark users by user_id, or external email
    addresses).
  - Times are ISO-8601 with timezone; if you pass a naive datetime we
    apply Asia/Singapore (operator's tz from notolark.yaml).

CLI:
  python tools/lark_calendar.py selftest                  # round-trip
  python tools/lark_calendar.py list-calendars
  python tools/lark_calendar.py list-events [--cal ID] [--days N]
  python tools/lark_calendar.py create-test               # makes + deletes a dummy
"""

import json
import os
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _base_url() -> str:
    from config import load_config
    return load_config()["lark"].get(
        "base_url", "https://open.larksuite.com").rstrip("/")


def _token() -> str:
    from lark_oauth import get_user_token
    return get_user_token("noah")


def _headers() -> Dict[str, str]:
    return {"Authorization": f"Bearer {_token()}",
            "Content-Type": "application/json"}


def _tz() -> str:
    try:
        from config import load_config
        return (load_config().get("system", {})
                .get("timezone", "Asia/Singapore"))
    except Exception:
        return "Asia/Singapore"


def _req(method: str, path: str, body: Optional[dict] = None
         ) -> Dict[str, Any]:
    url = f"{_base_url()}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=_headers(),
                                  method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body_txt = e.read().decode()
        raise RuntimeError(f"calendar {method} {path}: HTTP {e.code} "
                           f"{body_txt[:300]}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"calendar {method} {path}: {e}")


# ---------------------------------------------------------------------------
# Calendars
# ---------------------------------------------------------------------------

def list_calendars() -> List[Dict[str, Any]]:
    """Every calendar visible to Noah. Page through if needed."""
    out: List[Dict[str, Any]] = []
    page_token = ""
    while True:
        qs = "?page_size=50"
        if page_token:
            qs += f"&page_token={urllib.parse.quote(page_token)}"
        r = _req("GET", f"/open-apis/calendar/v4/calendars{qs}")
        d = r.get("data") or {}
        out.extend(d.get("calendar_list") or [])
        if not d.get("has_more"):
            break
        page_token = d.get("page_token") or ""
        if not page_token:
            break
    return out


def primary_calendar() -> Dict[str, Any]:
    """The user's own primary calendar (role='owner', type='primary')."""
    for c in list_calendars():
        if c.get("type") == "primary" and c.get("role") == "owner":
            return c
    # Fallback: first owned calendar
    for c in list_calendars():
        if c.get("role") == "owner":
            return c
    raise RuntimeError("noah has no owned calendar")


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

def list_events(calendar_id: str, start_ts: Optional[int] = None,
                end_ts: Optional[int] = None,
                page_size: int = 50) -> List[Dict[str, Any]]:
    """Events on one calendar between start_ts and end_ts (unix s).
    Default window: now → +30 days."""
    if start_ts is None:
        start_ts = int(time.time())
    if end_ts is None:
        end_ts = start_ts + 30 * 86400
    out: List[Dict[str, Any]] = []
    page_token = ""
    while True:
        params = {
            "start_time": str(start_ts),
            "end_time":   str(end_ts),
            "page_size":  str(page_size),
        }
        if page_token:
            params["page_token"] = page_token
        qs = "?" + urllib.parse.urlencode(params)
        r = _req("GET", f"/open-apis/calendar/v4/calendars/"
                        f"{urllib.parse.quote(calendar_id)}/events{qs}")
        d = r.get("data") or {}
        out.extend(d.get("items") or [])
        if not d.get("has_more"):
            break
        page_token = d.get("page_token") or ""
        if not page_token:
            break
    return out


def create_event(calendar_id: str, summary: str,
                 start: datetime, end: datetime,
                 description: str = "",
                 attendee_emails: Optional[List[str]] = None,
                 location: str = "",
                 reminders: Optional[List[int]] = None,
                 attendee_open_ids: Optional[List[str]] = None,
                 tz_name: str = "",
                 attendees_can_edit: bool = True
                 ) -> Dict[str, Any]:
    """Create an event. `start`/`end` are timezone-aware OR naive.
    Naive datetimes are localized to `tz_name` when given (user
    feedback 2026-07-06: '3 PM Eastern' was being scheduled at 3 PM
    Singapore — the requested timezone must survive end-to-end), else
    the configured default tz. attendees_can_edit sets Lark's
    attendee_ability=can_modify_event so invitees can move the event /
    add people afterwards. Returns the created event record."""
    tz = (tz_name or "").strip() or _tz()
    if start.tzinfo is None:
        try:
            from zoneinfo import ZoneInfo
            start = start.replace(tzinfo=ZoneInfo(tz))
            end = end.replace(tzinfo=ZoneInfo(tz))
        except Exception:
            # Fall back to fixed +08:00 (Asia/Singapore)
            offset = timezone(timedelta(hours=8))
            start = start.replace(tzinfo=offset)
            end = end.replace(tzinfo=offset)
    body: Dict[str, Any] = {
        "summary":     summary[:1000],
        "description": description[:8000] if description else "",
        "start_time": {
            "timestamp": str(int(start.timestamp())),
            "timezone":  tz,
        },
        "end_time": {
            "timestamp": str(int(end.timestamp())),
            "timezone":  tz,
        },
        "visibility":  "default",
        "color":       0,
        # invitees may edit (time/attendees) — user feedback
        "attendee_ability": ("can_modify_event" if attendees_can_edit
                             else "can_see_others"),
    }
    if location:
        body["location"] = {"name": location[:500]}
    if reminders:
        body["reminders"] = [{"minutes": int(m)} for m in reminders]
    r = _req("POST", f"/open-apis/calendar/v4/calendars/"
                     f"{urllib.parse.quote(calendar_id)}/events", body)
    event = (r.get("data") or {}).get("event") or {}
    event_id = event.get("event_id")
    # Attach attendees if any
    if (attendee_emails or attendee_open_ids) and event_id:
        try:
            add_attendees(calendar_id, event_id,
                          attendee_emails or [],
                          open_ids=attendee_open_ids or [])
        except Exception as e:
            # Don't fail event creation if attendee add fails;
            # surface via the returned event's `_attendee_error`.
            event["_attendee_error"] = str(e)
    return event


def add_attendees(calendar_id: str, event_id: str,
                  emails: List[str],
                  open_ids: Optional[List[str]] = None) -> Dict[str, Any]:
    """Add external email attendees to an event. Lark accepts either
    user_id (internal) or third_party_email (external). We default to
    external — callers can resolve internal user_ids first if they
    want a clean attendee record."""
    if not emails and not open_ids:
        return {}
    # HARD GUARD (operator rule 2026-07-06): email invitees must be on
    # the company domain (config lark.internal_email_domain). External
    # addresses are dropped and logged — NEVER invited, no matter which
    # caller passes them. Unset domain = no email invitees at all.
    from config import load_config
    _dom = ((load_config().get("lark") or {})
            .get("internal_email_domain", "") or "").lower().lstrip("@")
    kept = []
    for e in (emails or []):
        if _dom and str(e).lower().endswith("@" + _dom):
            kept.append(e)
        else:
            print(f"[lark_calendar] BLOCKED external invitee {e!r} "
                  f"(allowed domain: {_dom or '(none configured)'})",
                  file=sys.stderr, flush=True)
    emails = kept
    if not emails and not open_ids:
        return {}
    attendees = [{"type": "third_party", "third_party_email": e}
                 for e in (emails or [])]
    attendees += [{"type": "user", "user_id": oid}
                  for oid in (open_ids or [])]
    body = {"attendees": attendees}
    r = _req("POST", f"/open-apis/calendar/v4/calendars/"
                     f"{urllib.parse.quote(calendar_id)}/events/"
                     f"{urllib.parse.quote(event_id)}/attendees"
                     f"?user_id_type=open_id", body)
    return r.get("data") or {}


def freebusy(open_id: str, start: datetime, end: datetime
             ) -> List[Dict[str, Any]]:
    """Busy slots on a user's calendar between start and end (their
    whole calendar, not just ours) — used to warn about conflicts
    before adding an event for them. Returns [{start_time, end_time}]
    ISO strings; [] on failure or no access."""
    tz = _tz()
    def _iso(d: datetime) -> str:
        if d.tzinfo is None:
            try:
                from zoneinfo import ZoneInfo
                d = d.replace(tzinfo=ZoneInfo(tz))
            except Exception:
                d = d.replace(tzinfo=timezone(timedelta(hours=8)))
        return d.isoformat(timespec="seconds")
    try:
        r = _req("POST",
                 "/open-apis/calendar/v4/freebusy/list"
                 "?user_id_type=open_id",
                 {"time_min": _iso(start), "time_max": _iso(end),
                  "user_id": open_id})
        return ((r.get("data") or {}).get("freebusy_list")) or []
    except Exception as e:
        print(f"[lark_calendar] freebusy failed: {str(e)[:120]}",
              file=sys.stderr, flush=True)
        return []


def list_attendees(calendar_id: str, event_id: str
                   ) -> List[Dict[str, Any]]:
    r = _req("GET", f"/open-apis/calendar/v4/calendars/"
                    f"{urllib.parse.quote(calendar_id)}/events/"
                    f"{urllib.parse.quote(event_id)}/attendees")
    return ((r.get("data") or {}).get("items")) or []


def remove_attendees_by_email(calendar_id: str, event_id: str,
                              emails: List[str]) -> int:
    """Uninvite specific email attendees (they receive a standard
    cancellation). Used for remediation when an external address was
    invited by mistake — the domain guard in add_attendees prevents
    new occurrences."""
    want = {e.lower() for e in emails}
    ids = [a.get("attendee_id") for a in
           list_attendees(calendar_id, event_id)
           if (a.get("third_party_email") or "").lower() in want
           and a.get("attendee_id")]
    if not ids:
        return 0
    _req("POST", f"/open-apis/calendar/v4/calendars/"
                 f"{urllib.parse.quote(calendar_id)}/events/"
                 f"{urllib.parse.quote(event_id)}/attendees/batch_delete",
         {"attendee_ids": ids})
    return len(ids)


def get_event(calendar_id: str, event_id: str) -> Dict[str, Any]:
    r = _req("GET", f"/open-apis/calendar/v4/calendars/"
                    f"{urllib.parse.quote(calendar_id)}/events/"
                    f"{urllib.parse.quote(event_id)}")
    return (r.get("data") or {}).get("event") or {}


def update_event(calendar_id: str, event_id: str,
                 patch: Dict[str, Any]) -> Dict[str, Any]:
    """Patch an existing event. `patch` is a partial event dict
    (e.g. {'summary': 'new title'}). Lark requires PATCH semantics."""
    r = _req("PATCH", f"/open-apis/calendar/v4/calendars/"
                      f"{urllib.parse.quote(calendar_id)}/events/"
                      f"{urllib.parse.quote(event_id)}", patch)
    return (r.get("data") or {}).get("event") or {}


def delete_event(calendar_id: str, event_id: str) -> bool:
    """Cancel an event. Lark soft-deletes per their docs (event stays
    on the calendar marked cancelled) so this is reversible. Used by
    selftest cleanup + admin undo flows."""
    r = _req("DELETE", f"/open-apis/calendar/v4/calendars/"
                       f"{urllib.parse.quote(calendar_id)}/events/"
                       f"{urllib.parse.quote(event_id)}")
    return r.get("code") == 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _selftest() -> int:
    """End-to-end: list calendars, find primary, create a dummy event
    5 min from now, fetch it back, delete it. Asserts each step."""
    print("→ list calendars…")
    cals = list_calendars()
    print(f"  {len(cals)} visible")
    print("→ find primary…")
    p = primary_calendar()
    print(f"  primary: {p.get('summary','?')!r}  id={p['calendar_id'][:24]}…")
    print("→ create dummy event…")
    now = datetime.now()
    start = now + timedelta(minutes=5)
    end = start + timedelta(minutes=30)
    ev = create_event(p["calendar_id"],
                      summary="Noto selftest — safe to delete",
                      description="Created by lark_calendar.py selftest. "
                                  "Will be deleted in ~2 seconds.",
                      start=start, end=end)
    eid = ev.get("event_id")
    assert eid, f"create failed: {ev}"
    print(f"  created event_id={eid[:24]}…")
    print("→ fetch back…")
    got = get_event(p["calendar_id"], eid)
    assert got.get("summary","").startswith("Noto selftest"), \
        f"summary mismatch: {got}"
    print(f"  fetched, summary={got['summary']!r}")
    print("→ delete…")
    ok = delete_event(p["calendar_id"], eid)
    assert ok, "delete returned False"
    print("  deleted ✓")
    print("\nALL PASS")
    return 0


def main(argv: List[str]) -> int:
    if not argv:
        print(__doc__.strip())
        return 0
    cmd = argv[0]
    if cmd == "selftest":
        return _selftest()
    if cmd == "list-calendars":
        for c in list_calendars():
            print(f"  [{c.get('role','?'):5}] "
                  f"{(c.get('summary') or '(unnamed)')[:50]:50}  "
                  f"id={c.get('calendar_id','')[:30]}…  "
                  f"type={c.get('type','?')}")
        return 0
    if cmd == "list-events":
        cal = None
        days = 7
        for i, a in enumerate(argv):
            if a == "--cal" and i + 1 < len(argv):
                cal = argv[i + 1]
            if a == "--days" and i + 1 < len(argv):
                days = int(argv[i + 1])
        if not cal:
            cal = primary_calendar()["calendar_id"]
        end_ts = int(time.time()) + days * 86400
        for e in list_events(cal, end_ts=end_ts):
            st = e.get("start_time", {}).get("timestamp", "?")
            print(f"  {st}  {(e.get('summary') or '')[:60]}")
        return 0
    if cmd == "create-test":
        p = primary_calendar()
        start = datetime.now() + timedelta(hours=1)
        end = start + timedelta(minutes=30)
        ev = create_event(p["calendar_id"],
                          summary="Noto test event (delete me)",
                          start=start, end=end)
        print(json.dumps(ev, indent=2)[:500])
        return 0
    print("commands: selftest | list-calendars | list-events [--cal ID] "
          "[--days N] | create-test", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
