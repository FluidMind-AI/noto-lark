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
"end_time","location","meeting_link","is_physical","notes"). If the
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


def create_for(requester_open_id: str, ev: Dict[str, Any]
               ) -> Dict[str, Any]:
    """Create the event on the primary calendar and invite the
    requester by open_id so it lands on THEIR calendar, with 60- and
    15-minute reminders."""
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
            attendee_open_ids=[requester_open_id])
        return {"ok": bool(event.get("event_id")),
                "event_id": event.get("event_id"),
                "attendee_error": event.get("_attendee_error"),
                "start": start, "end": end}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


def confirmation(ev: Dict[str, Any], res: Dict[str, Any]) -> str:
    when = f"{ev.get('date')} {ev.get('start_time')}"
    txt = (f"📅 Added to your calendar: **{ev.get('title')}** — {when}"
           + (f", {ev['location']}" if ev.get("location") else "")
           + "\nReminders set for 1 hour and 15 minutes before.")
    if res.get("attendee_error"):
        txt += ("\n⚠️ I created the event but couldn't add you as "
                "attendee — check the shared calendar.")
    return txt
