#!/usr/bin/env python3
"""
Screenshot → calendar entry (operator ask, 2026-07-05).

A user DMs Noto a screenshot (an email, a chat, a poster —
anything with meeting details). Vision reads it; if it's event-shaped,
Noto builds a calendar entry FOR THE REQUESTER (invited by open_id —
lands on their own calendar) with reminders at 60 and 15 minutes
before. If details are missing, Noto asks and merges the answers; a
PHYSICAL meeting additionally requires a location. Invitees in the
screenshot are ignored by design.

The same vision pass classifies receipts, so the expenses flow keeps
working: lark_bot routes kind='receipt' to expenses on the transcribed
text, kind='event' here, anything else gets a "here's what I see —
what would you like?" reply.

Flag: h2.screenshot_calendar_enabled.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

sys.path.insert(0, __file__.rsplit("/", 1)[0])

REMINDER_MINUTES = [60, 15]
PENDING_TTL_S = 15 * 60


def enabled() -> bool:
    from config import load_config
    return bool((load_config().get("h2") or {})
                .get("screenshot_calendar_enabled", False))


_VISION_PROMPT = """Read the image file {fname} in the current directory.

TODAY is {today}. Classify what the image shows and reply with ONLY a
JSON object (no prose, no fences):

{{"kind": "event" | "receipt" | "other",
 "description": "<one line: what the image shows>",
 "receipt_text": "<ONLY if kind=receipt: transcribe merchant, line
items, currency, total, date as plain text; else null>",
 "event": <ONLY if kind=event, else null:
   {{"title": "<meeting/event title, or null>",
     "date": "<YYYY-MM-DD; resolve weekday words against TODAY; null
if not determinable>",
     "start_time": "<HH:MM 24h, or null>",
     "end_time": "<HH:MM 24h, or null>",
     "timezone": "<IANA zone like America/New_York IF a timezone is
named or implied (ET/EST/PT/HKT/JST/'NY time'…), else null>",
     "location": "<address/room/venue, or null>",
     "meeting_link": "<zoom/teams/meet URL if shown, or null>",
     "is_physical": <true if it's an in-person meeting (venue/address/
office mentioned, no video link), false if clearly virtual, null if
unclear>,
     "notes": "<anything else useful from the screenshot>"}}>,
 "confidence": <0-1>}}

kind=event when the image is about a meeting/call/appointment/dinner/
interview to attend. kind=receipt when it's a purchase receipt or
invoice. Anything else (a CV, a chart, a random photo) is "other".
"""


def analyze_image(blob: bytes, filename: str = "shot.png",
                  hint: str = "") -> Dict[str, Any]:
    """Vision classify + extract. The claude CLI gets ONE tool (Read)
    and a cwd pinned to a scratch dir holding only this image."""
    from noto_research import _claude_bin, _bot_model
    today = datetime.now().strftime("%Y-%m-%d (%A)")
    with tempfile.TemporaryDirectory(prefix="noto-shot-") as d:
        fname = os.path.basename(filename) or "shot.png"
        if not fname.lower().endswith((".png", ".jpg", ".jpeg",
                                       ".webp", ".gif", ".heic")):
            fname += ".png"
        with open(os.path.join(d, fname), "wb") as f:
            f.write(blob)
        try:
            prompt = _VISION_PROMPT.format(fname=fname, today=today)
            if hint.strip():
                prompt += (f"\nTHE SENDER SAID WITH THE IMAGE: "
                           f"\"{hint.strip()[:300]}\" — weigh this when "
                           f"classifying (e.g. 'add to my calendar' means "
                           f"treat it as an event even if informal).")
            res = subprocess.run(
                [_claude_bin(), "-p", prompt,
                 "--allowedTools", "Read", "--model", _bot_model()],
                cwd=d, capture_output=True, text=True, timeout=150,
                stdin=subprocess.DEVNULL)
            raw = (res.stdout or "").strip()
        except Exception as e:
            return {"kind": "error", "error": str(e)[:150]}
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return {"kind": "error", "error": "vision returned no JSON"}
    try:
        out = json.loads(m.group(0))
        return out if isinstance(out, dict) else {"kind": "error"}
    except Exception:
        return {"kind": "error", "error": "vision JSON unparseable"}


def missing_fields(ev: Dict[str, Any]) -> List[str]:
    """What's still needed before the entry can be created."""
    missing = []
    if not (ev.get("title") or "").strip():
        missing.append("title")
    if not (ev.get("date") or "").strip():
        missing.append("date")
    if not (ev.get("start_time") or "").strip():
        missing.append("start_time")
    if ev.get("is_physical") and not (ev.get("location") or "").strip():
        missing.append("location")
    if ev.get("is_physical") is None and \
            not (ev.get("location") or "").strip() and \
            not (ev.get("meeting_link") or "").strip():
        missing.append("physical_or_virtual")
    return missing


_FIELD_QUESTION = {
    "title": "what should the entry be called?",
    "date": "what date is it?",
    "start_time": "what time does it start?",
    "location": "it looks like an in-person meeting — where is it?",
    "physical_or_virtual": "is this in person or a video/phone call? "
                           "(if in person, where?)",
}


def question_for(missing: List[str], ev: Dict[str, Any]) -> str:
    got = []
    if ev.get("title"):
        got.append(f"“{ev['title']}”")
    if ev.get("date"):
        got.append(ev["date"])
    if ev.get("start_time"):
        got.append(ev["start_time"])
    head = ("📅 I can add that to your calendar"
            + (f" ({', '.join(got)})" if got else "") + ", but ")
    qs = [_FIELD_QUESTION.get(mf, mf) for mf in missing[:3]]
    return head + "I still need: " + " and ".join(qs) + \
        "\n(reply here, or say cancel)"


_TEXT_PROMPT = """A user wants a calendar entry created from \
their message. TODAY is {today}.

THEIR MESSAGE:
```
{text}
```

Extract the event. Reply with ONLY a JSON object:
{{"is_event": <true if this is a request to schedule/add something>,
 "title": "<e.g. 'Dinner with Joe Kim', or null>",
 "date": "<YYYY-MM-DD, resolving weekday words against TODAY; null if
not given>",
 "start_time": "<HH:MM 24h, or null>",
 "end_time": "<HH:MM 24h, or null>",
 "timezone": "<IANA zone like America/New_York IF named or implied
(ET/PT/HKT/'3pm Eastern'…), else null>",
 "location": "<venue/address if given, or null>",
 "meeting_link": "<URL if given, or null>",
 "is_physical": <true for dinners/coffees/venues, false for calls/
video, null if unclear>,
 "notes": "<anything else from the message>"}}
"""


def extract_from_text(text: str) -> Optional[Dict[str, Any]]:
    """Event fields from a plain chat request ('add to my calendar
    dinner with Joe Kim on Wednesday 8pm at COTE'). Same downstream
    flow as screenshots: missing-field questions, clash check,
    reminders."""
    from noto_research import _claude
    today = datetime.now().strftime("%Y-%m-%d (%A)")
    raw = _claude(_TEXT_PROMPT.format(today=today, text=(text or "")[:1500]),
                  timeout=60, web=False) or ""
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return None
    try:
        out = json.loads(m.group(0))
        return out if isinstance(out, dict) else None
    except Exception:
        return None


_MERGE_PROMPT = """A user is completing a calendar entry. TODAY \
is {today}.

Current fields (null = missing):
{fields}

Their reply:
```
{answer}
```

Merge the reply into the fields. Reply with ONLY the full updated JSON
object, same keys ("title","date" YYYY-MM-DD,"start_time" HH:MM,
"end_time","timezone" IANA-or-null,"location","meeting_link",
"is_physical","notes"). If the
reply says to cancel/stop, add "cancelled": true.
"""


def merge_answer(ev: Dict[str, Any], answer: str) -> Dict[str, Any]:
    from noto_research import _claude
    today = datetime.now().strftime("%Y-%m-%d (%A)")
    raw = _claude(_MERGE_PROMPT.format(
        today=today, fields=json.dumps(ev, indent=1),
        answer=answer[:800]), timeout=60, web=False) or ""
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return ev
    try:
        out = json.loads(m.group(0))
        return out if isinstance(out, dict) else ev
    except Exception:
        return ev


def _parse_times(ev: Dict[str, Any]):
    start = datetime.strptime(
        f"{ev['date']} {ev['start_time']}", "%Y-%m-%d %H:%M")
    end = start + timedelta(hours=1)
    if ev.get("end_time"):
        try:
            end = datetime.strptime(
                f"{ev['date']} {ev['end_time']}", "%Y-%m-%d %H:%M")
            if end <= start:
                end = start + timedelta(hours=1)
        except Exception:
            pass
    return start, end


def check_clashes(requester_open_id: str, ev: Dict[str, Any]
                  ) -> Dict[str, Any]:
    """Duplicate + conflict check before creating (operator ask,
    2026-07-05): (1) an event with the same title-ish overlapping the
    same slot on the bot calendar = DUPLICATE; (2) anything already
    busy on the REQUESTER's calendar in that window (via freebusy —
    their whole calendar, not just ours) = CONFLICT. Returns
    {duplicate: str|None, conflicts: [(start,end)], ok: bool}."""
    out: Dict[str, Any] = {"duplicate": None, "conflicts": []}
    try:
        start, end = _parse_times(ev)
        # localize to the event's tz so absolute-time comparison is
        # right even for '3pm Eastern' proposals
        tzn = event_tz(requester_open_id, ev)
        if tzn:
            try:
                from zoneinfo import ZoneInfo
                start = start.replace(tzinfo=ZoneInfo(tzn))
                end = end.replace(tzinfo=ZoneInfo(tzn))
            except Exception:
                pass
    except Exception:
        return out
    title_toks = {t for t in re.findall(
        r"[a-z0-9]{3,}", (ev.get("title") or "").lower())}
    try:
        from lark_calendar import primary_calendar, list_events
        import time as _t
        day0 = int(start.replace(hour=0, minute=0).timestamp())
        evs = list_events(primary_calendar()["calendar_id"],
                          start_ts=day0, end_ts=day0 + 86400)
        for e in evs:
            st = e.get("start_time") or {}
            ets = int(st.get("timestamp") or 0)
            ee = e.get("end_time") or {}
            ete = int(ee.get("timestamp") or 0)
            if not ets or (e.get("status") or "") == "cancelled":
                continue
            overlap = ets < end.timestamp() and                 (ete or ets + 3600) > start.timestamp()
            if not overlap:
                continue
            e_toks = {t for t in re.findall(
                r"[a-z0-9]{3,}", (e.get("summary") or "").lower())}
            if title_toks and e_toks and                     len(title_toks & e_toks) / len(title_toks) >= 0.6:
                out["duplicate"] = (f"{e.get('summary','?')} at "
                                    f"{datetime.fromtimestamp(ets):%H:%M}")
                break
    except Exception:
        pass
    try:
        # Lark freebusy quirks (verified empirically 2026-07-05):
        # narrow windows return [], so query the WHOLE local day and
        # intersect here; and the timestamps are TRUE UTC ('Z') even
        # though events were created in Asia/Singapore — convert
        # explicitly (the process TZ is not trustworthy).
        from lark_calendar import freebusy, _tz
        try:
            from zoneinfo import ZoneInfo
            zone = ZoneInfo(_tz())
        except Exception:
            from datetime import timezone as _tzm
            zone = _tzm(timedelta(hours=8))
        day_s = start.replace(hour=0, minute=0)
        day_e = start.replace(hour=23, minute=59)
        for b in freebusy(requester_open_id, day_s, day_e):
            try:
                bs = datetime.fromisoformat(
                    (b.get("start_time") or "").replace("Z", "+00:00"))
                be = datetime.fromisoformat(
                    (b.get("end_time") or "").replace("Z", "+00:00"))
            except Exception:
                continue
            bs_n = bs.astimezone(zone).replace(tzinfo=None)
            be_n = be.astimezone(zone).replace(tzinfo=None)
            if bs_n < end and be_n > start:      # overlaps proposal
                out["conflicts"].append(
                    (f"{bs_n:%Y-%m-%d %H:%M}", f"{be_n:%H:%M}"))
    except Exception:
        pass
    out["conflicts"] = sorted(set(out["conflicts"]))
    return out


def clash_question(clash: Dict[str, Any], ev: Dict[str, Any]) -> str:
    if clash.get("duplicate"):
        return (f"⚠️ This looks like it's ALREADY on the calendar: "
                f"**{clash['duplicate']}** on {ev.get('date')}.\n"
                f"Add it anyway, or skip? (reply 'add anyway' / 'skip')")
    lines = "\n".join(f"  • {s} → {e}" for s, e in
                       clash.get("conflicts", [])[:4])
    return (f"⚠️ You already have something in that slot on "
            f"{ev.get('date')}:\n{lines}\n"
            f"Add **{ev.get('title')}** ({ev.get('start_time')}) anyway, "
            f"or skip? (reply 'add anyway' / 'skip')")


def _user_tz(open_id: str) -> str:
    """The requester's home timezone: operators.yaml `timezone:` per
    person, else the org default. (Operator rule 2026-07-06: when the
    request names no timezone, assume the USER's, not the server's.)"""
    try:
        import yaml, os
        from config import get_home
        ops = yaml.safe_load(open(os.path.join(
            get_home(), "memory", "operators.yaml"))) or {}
        for v in ops.values():
            if isinstance(v, dict) and v.get("open_id") == open_id \
                    and v.get("timezone"):
                return str(v["timezone"])
    except Exception:
        pass
    return ""


# City -> IANA tz for travel-event detection. Extend freely.
_CITY_TZ = {
    "singapore": "Asia/Singapore", "hong kong": "Asia/Hong_Kong",
    "hk": "Asia/Hong_Kong", "tokyo": "Asia/Tokyo", "osaka": "Asia/Tokyo",
    "seoul": "Asia/Seoul", "shanghai": "Asia/Shanghai",
    "beijing": "Asia/Shanghai", "taipei": "Asia/Taipei",
    "manila": "Asia/Manila", "bangkok": "Asia/Bangkok",
    "jakarta": "Asia/Jakarta", "kuala lumpur": "Asia/Kuala_Lumpur",
    "kl": "Asia/Kuala_Lumpur", "sydney": "Australia/Sydney",
    "melbourne": "Australia/Melbourne", "dubai": "Asia/Dubai",
    "abu dhabi": "Asia/Dubai", "doha": "Asia/Qatar",
    "riyadh": "Asia/Riyadh", "mumbai": "Asia/Kolkata",
    "delhi": "Asia/Kolkata", "london": "Europe/London",
    "paris": "Europe/Paris", "frankfurt": "Europe/Berlin",
    "munich": "Europe/Berlin", "berlin": "Europe/Berlin",
    "madrid": "Europe/Madrid", "milan": "Europe/Rome",
    "rome": "Europe/Rome", "zurich": "Europe/Zurich",
    "geneva": "Europe/Zurich", "amsterdam": "Europe/Amsterdam",
    "new york": "America/New_York", "nyc": "America/New_York",
    "ny": "America/New_York", "atlanta": "America/New_York",
    "miami": "America/New_York", "boston": "America/New_York",
    "washington": "America/New_York", "dc": "America/New_York",
    "chicago": "America/Chicago", "houston": "America/Chicago",
    "dallas": "America/Chicago", "toronto": "America/Toronto",
    "vancouver": "America/Vancouver", "seattle": "America/Los_Angeles",
    "san francisco": "America/Los_Angeles", "sf": "America/Los_Angeles",
    "los angeles": "America/Los_Angeles", "la": "America/Los_Angeles",
    "mexico city": "America/Mexico_City", "bogota": "America/Bogota",
    "bogotá": "America/Bogota", "medellin": "America/Bogota",
    "medellín": "America/Bogota", "sao paulo": "America/Sao_Paulo",
    "são paulo": "America/Sao_Paulo",
}

_TRAVEL_RE = re.compile(
    r"(?:travel(?:ing|ling)?(?:\s+to)?|trip\s+to|flying\s+to|"
    r"fly\s+to|in|at|@|✈️?\s*)\s*[:\-]?\s*([A-Za-zÀ-ÿ .]{2,20})\s*$",
    re.I)

_cal_cache: Dict[str, Any] = {}


def _travel_tz(open_id: str, date_str: str) -> str:
    """If the person has a travel-shaped event on their SHARED calendar
    covering `date_str` ('Travel: Tokyo', 'Trip to London', '✈️ HK',
    'In New York'), return that city's timezone. Requires the person to
    share their calendar with the bot user (Reader is enough); '' when
    no access or no travel found. (Operator rule 2026-07-08: check for
    travel before assuming someone's home timezone.)"""
    try:
        import yaml, os, time as _t
        from config import get_home
        from lark_calendar import list_calendars, list_events
        # person's display name
        ops = yaml.safe_load(open(os.path.join(
            get_home(), "memory", "operators.yaml"))) or {}
        name = next((v.get("name") for v in ops.values()
                     if isinstance(v, dict)
                     and v.get("open_id") == open_id), "")
        if not name:
            return ""
        first = name.split()[0].lower()
        # calendar list is cached 10 min (it's one API call otherwise)
        if _cal_cache.get("ts", 0) < _t.time() - 600:
            _cal_cache["cals"] = list_calendars()
            _cal_cache["ts"] = _t.time()
        cal = next((c for c in _cal_cache.get("cals", [])
                    if first in (c.get("summary") or "").lower()
                    and c.get("role") in ("reader", "writer", "owner")),
                   None)
        if not cal:
            return ""
        day = datetime.strptime(date_str, "%Y-%m-%d")
        evs = list_events(cal["calendar_id"],
                          start_ts=int(day.timestamp()),
                          end_ts=int(day.timestamp()) + 86400)
        for e in evs:
            title = (e.get("summary") or "").strip()
            # The travel phrase must be about the CALENDAR OWNER:
            # anchored at the start of the title, optionally after the
            # owner's own first name ("Sharmaine in Tokyo" on her own
            # calendar). "Sheldon in London" on Sharmaine's calendar is
            # about someone else — live test caught exactly that.
            probe = title
            if probe.lower().startswith(first):
                probe = probe[len(first):].lstrip(" :–-")
            m = _TRAVEL_RE.match(probe)
            if not m:
                continue
            city = m.group(1).strip().lower().rstrip(".")
            tz = _CITY_TZ.get(city)
            if tz:
                print(f"[screenshot_calendar] travel detected for "
                      f"{name}: {title!r} -> {tz}", file=sys.stderr,
                      flush=True)
                return tz
    except Exception:
        pass
    return ""


def event_tz(requester_open_id: str, ev: Dict[str, Any]) -> str:
    """Timezone priority (operator rules 2026-07-06/08):
    named in the request > travel event on the requester's shared
    calendar for that date > requester's home tz (operators.yaml) >
    org default (create_event resolves '' to the configured tz)."""
    named = (ev.get("timezone") or "").strip()
    if named:
        return named
    if ev.get("date"):
        t = _travel_tz(requester_open_id, ev["date"])
        if t:
            return t
    return _user_tz(requester_open_id)


def create_for(requester_open_id: str, ev: Dict[str, Any]
               ) -> Dict[str, Any]:
    """Create the event on the primary calendar and invite the
    requester by open_id so it lands on THEIR calendar, with 60- and
    15-minute reminders. Times are interpreted in event_tz() and the
    event is attendee-editable."""
    try:
        from lark_calendar import primary_calendar, create_event
        start, end = _parse_times(ev)
        desc_bits = []
        if ev.get("meeting_link"):
            desc_bits.append(f"Join: {ev['meeting_link']}")
        if ev.get("notes"):
            desc_bits.append(str(ev["notes"])[:500])
        desc_bits.append("Added by Noto from a screenshot.")
        event = create_event(
            calendar_id=primary_calendar()["calendar_id"],
            summary=(ev.get("title") or "Meeting")[:200],
            start=start, end=end,
            description="\n".join(desc_bits),
            location=(ev.get("location") or "")[:300],
            reminders=REMINDER_MINUTES,
            attendee_open_ids=[requester_open_id],
            tz_name=event_tz(requester_open_id, ev),
            attendees_can_edit=True)
        return {"ok": bool(event.get("event_id")),
                "event_id": event.get("event_id"),
                "attendee_error": event.get("_attendee_error"),
                "tz_used": event_tz(requester_open_id, ev),
                "start": start, "end": end}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


def confirmation(ev: Dict[str, Any], res: Dict[str, Any]) -> str:
    when = f"{ev.get('date')} {ev.get('start_time')}"
    tzn = (res.get("tz_used") or ev.get("timezone") or "").strip()
    if tzn:
        when += f" ({tzn.split('/')[-1].replace('_', ' ')} time)"
    txt = (f"📅 Added to your calendar: **{ev.get('title')}** — {when}"
           + (f", {ev['location']}" if ev.get("location") else "")
           + "\nReminders set for 1 hour and 15 minutes before.")
    if res.get("attendee_error"):
        txt += ("\n⚠️ I created the event but couldn't add you as "
                "attendee — check the shared calendar.")
    return txt
