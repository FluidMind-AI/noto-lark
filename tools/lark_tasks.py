"""
Lark Tasks — thin wrapper around /open-apis/task/v2 (Task v2).

Backs the per-user reminder lists: every teammate gets ONE tasklist
named "Noto — <Name>", created by the Noah service user (so Noto can
always manage it) with the teammate added as an editor member — that
membership is what makes the list show up in THEIR Lark Tasks app.
A reminder is a task in that list with the teammate as assignee (Lark
renders the assignee as the task's Owner) and, when a specific time
was given, a due timestamp + an at-due-time alert that Lark pushes
natively — no bot polling involved.

NO DELETE CAPABILITY — by design (Lark Data Safety rule; this file is
scanned by lark_client.assert_no_lark_delete). Reminders are only ever
completed (PATCH completed_at), never removed.

Scopes (identity "noah" — see lark_oauth.NOAH_SCOPES and
docs/setup.md): task:task:read task:task:write
task:tasklist:read task:tasklist:write

CLI:
  python tools/lark_tasks.py selftest                    # round-trip
  python tools/lark_tasks.py ensure-list --open-id OU --name NAME
  python tools/lark_tasks.py add --open-id OU --summary S [--due-ms MS]
  python tools/lark_tasks.py list --open-id OU [--all]
  python tools/lark_tasks.py complete --guid TASK_GUID
"""

import json
import os
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
import uuid
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
        raise RuntimeError(f"tasks {method} {path}: HTTP {e.code} "
                           f"{body_txt[:300]}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"tasks {method} {path}: {e}")


# ---------------------------------------------------------------------------
# Per-user tasklist registry (brain/reminder-tasklists.json)
# ---------------------------------------------------------------------------

def _state_path() -> str:
    from config import get_home
    return os.path.join(get_home(), "brain", "reminder-tasklists.json")


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


def user_tasklist_guid(open_id: str) -> Optional[str]:
    """Registered tasklist guid for a user, or None. Never creates —
    use ensure_user_tasklist for that (digest wants the no-create
    lookup so it doesn't spawn lists for people who never asked)."""
    rec = _load_state().get(open_id) or {}
    return rec.get("guid") or None


# ---------------------------------------------------------------------------
# Tasklists
# ---------------------------------------------------------------------------

def get_tasklist(tasklist_guid: str) -> Dict[str, Any]:
    r = _req("GET", f"/open-apis/task/v2/tasklists/"
                    f"{urllib.parse.quote(tasklist_guid)}")
    return (r.get("data") or {}).get("tasklist") or {}


def create_tasklist(name: str, member_open_ids: List[str]
                    ) -> Dict[str, Any]:
    """Create a tasklist owned by Noah with the given users as editor
    members (editor = they can add/complete their own items from the
    Lark Tasks app directly)."""
    body = {
        "name": name[:100],
        "members": [{"id": oid, "type": "user", "role": "editor"}
                    for oid in member_open_ids],
    }
    r = _req("POST", "/open-apis/task/v2/tasklists?user_id_type=open_id",
             body)
    return (r.get("data") or {}).get("tasklist") or {}


def add_tasklist_members(tasklist_guid: str, open_ids: List[str],
                         role: str = "editor") -> None:
    if not open_ids:
        return
    _req("POST", f"/open-apis/task/v2/tasklists/"
                 f"{urllib.parse.quote(tasklist_guid)}/add_members"
                 f"?user_id_type=open_id",
         {"members": [{"id": oid, "type": "user", "role": role}
                      for oid in open_ids]})


def ensure_user_tasklist(open_id: str, display_name: str) -> str:
    """Guid of the user's "Noto — <Name>" tasklist, creating it (and
    registering it in brain/reminder-tasklists.json) on first use.
    A registered guid is verified against Lark and re-created if the
    list is gone (users CAN delete their own lists from the Tasks app;
    Noto itself never does)."""
    state = _load_state()
    rec = state.get(open_id) or {}
    guid = rec.get("guid")
    if guid:
        try:
            if get_tasklist(guid).get("guid") == guid:
                return guid
        except RuntimeError:
            pass  # gone or inaccessible — fall through to re-create
    name = f"Noto — {display_name.strip() or 'Reminders'}"
    tl = create_tasklist(name, [open_id])
    guid = tl.get("guid") or ""
    if not guid:
        raise RuntimeError(f"tasklist create returned no guid: {tl}")
    state[open_id] = {"guid": guid, "name": name,
                      "created_at": int(time.time())}
    _save_state(state)
    return guid


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------

def create_task(open_id: str, summary: str,
                display_name: str = "",
                due_ts_ms: Optional[int] = None,
                is_all_day: bool = False,
                description: str = "",
                tasklist_guid: str = "") -> Dict[str, Any]:
    """Create a reminder task in the user's Noto list with the user as
    assignee (shown as Owner in the Lark UI). A timed due date gets an
    at-due-time alert (relative_fire_minute=0); all-day and undated
    reminders rely on the morning digest instead."""
    guid = tasklist_guid or ensure_user_tasklist(open_id, display_name)
    body: Dict[str, Any] = {
        "summary": summary[:3000],
        "members": [{"id": open_id, "type": "user", "role": "assignee"}],
        "tasklists": [{"tasklist_guid": guid}],
        "client_token": str(uuid.uuid4()),
    }
    if description:
        body["description"] = description[:3000]
    if due_ts_ms:
        body["due"] = {"timestamp": str(int(due_ts_ms)),
                       "is_all_day": bool(is_all_day)}
        if not is_all_day:
            body["reminders"] = [{"relative_fire_minute": 0}]
    r = _req("POST", "/open-apis/task/v2/tasks?user_id_type=open_id",
             body)
    task = (r.get("data") or {}).get("task") or {}
    task["_tasklist_guid"] = guid
    return task


def list_tasks(tasklist_guid: str, completed: Optional[bool] = False
               ) -> List[Dict[str, Any]]:
    """Tasks in a tasklist. completed=False (default) → open items
    only; True → done items; None → everything."""
    out: List[Dict[str, Any]] = []
    page_token = ""
    while True:
        params = {"page_size": "100"}
        if completed is not None:
            params["completed"] = "true" if completed else "false"
        if page_token:
            params["page_token"] = page_token
        qs = "?" + urllib.parse.urlencode(params)
        r = _req("GET", f"/open-apis/task/v2/tasklists/"
                        f"{urllib.parse.quote(tasklist_guid)}/tasks{qs}")
        d = r.get("data") or {}
        out.extend(d.get("items") or [])
        if not d.get("has_more"):
            break
        page_token = d.get("page_token") or ""
        if not page_token:
            break
    return out


def get_task(task_guid: str) -> Dict[str, Any]:
    r = _req("GET", f"/open-apis/task/v2/tasks/"
                    f"{urllib.parse.quote(task_guid)}")
    return (r.get("data") or {}).get("task") or {}


def complete_task(task_guid: str) -> Dict[str, Any]:
    """Mark a task done (sets completed_at=now). The only mutation
    Noto performs on existing tasks — never removal."""
    now_ms = int(time.time() * 1000)
    r = _req("PATCH", f"/open-apis/task/v2/tasks/"
                      f"{urllib.parse.quote(task_guid)}",
             {"task": {"completed_at": str(now_ms)},
              "update_fields": ["completed_at"]})
    return (r.get("data") or {}).get("task") or {}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _selftest() -> int:
    """Round-trip against the Noah service user's own open_id: ensure
    a selftest list, add a task due in 5 min, list it back, complete
    it. Leaves the (empty) selftest list behind — tasklists are never
    removed, and the registry reuses it on the next run."""
    import yaml
    from config import get_home
    ops = yaml.safe_load(open(os.path.join(
        get_home(), "memory", "operators.yaml"))) or {}
    noah = next((v for v in ops.values()
                 if v.get("role") == "service_account"), None)
    assert noah and noah.get("open_id"), "no service_account in operators.yaml"
    oid = noah["open_id"]
    print("→ ensure selftest tasklist…")
    guid = ensure_user_tasklist(oid, "Selftest")
    print(f"  guid={guid[:24]}…")
    print("→ create task due in 5 min…")
    due = int(time.time() * 1000) + 5 * 60 * 1000
    t = create_task(oid, "Noto selftest — safe to complete",
                    due_ts_ms=due, tasklist_guid=guid)
    tg = t.get("guid")
    assert tg, f"create failed: {t}"
    print(f"  task guid={tg[:24]}…")
    print("→ list open tasks…")
    items = list_tasks(guid, completed=False)
    assert any(i.get("guid") == tg for i in items), \
        f"created task not in list ({len(items)} items)"
    print(f"  found among {len(items)} open item(s)")
    print("→ complete…")
    done = complete_task(tg)
    assert (done.get("completed_at") or "0") != "0", \
        f"complete failed: {done}"
    print("  completed ✓")
    print("\nALL PASS")
    return 0


def _arg(argv: List[str], name: str, default: str = "") -> str:
    for i, a in enumerate(argv):
        if a == name and i + 1 < len(argv):
            return argv[i + 1]
    return default


def main(argv: List[str]) -> int:
    if not argv:
        print(__doc__.strip())
        return 0
    cmd = argv[0]
    if cmd == "selftest":
        return _selftest()
    if cmd == "ensure-list":
        oid = _arg(argv, "--open-id")
        name = _arg(argv, "--name", "Reminders")
        if not oid:
            print("need --open-id", file=sys.stderr)
            return 2
        print(ensure_user_tasklist(oid, name))
        return 0
    if cmd == "add":
        oid = _arg(argv, "--open-id")
        summary = _arg(argv, "--summary")
        if not oid or not summary:
            print("need --open-id and --summary", file=sys.stderr)
            return 2
        due = _arg(argv, "--due-ms")
        t = create_task(oid, summary,
                        due_ts_ms=int(due) if due else None)
        print(json.dumps(t, indent=2)[:800])
        return 0
    if cmd == "list":
        oid = _arg(argv, "--open-id")
        guid = user_tasklist_guid(oid) if oid else ""
        if not guid:
            print("(no tasklist registered for that open_id)")
            return 0
        completed = None if "--all" in argv else False
        for t in list_tasks(guid, completed=completed):
            due = (t.get("due") or {}).get("timestamp", "")
            mark = "✓" if (t.get("completed_at") or "0") != "0" else "·"
            print(f"  {mark} {(t.get('summary') or '')[:60]:60} "
                  f"due={due or '—'}  guid={t.get('guid','')[:20]}…")
        return 0
    if cmd == "complete":
        guid = _arg(argv, "--guid")
        if not guid:
            print("need --guid", file=sys.stderr)
            return 2
        print(json.dumps(complete_task(guid), indent=2)[:500])
        return 0
    print("commands: selftest | ensure-list --open-id OU --name NAME | "
          "add --open-id OU --summary S [--due-ms MS] | "
          "list --open-id OU [--all] | complete --guid TASK_GUID",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
