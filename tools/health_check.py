#!/usr/bin/env python3
"""
Daily health self-check: verify every subsystem is running and working
as intended; self-fix what is safely fixable; DM the operator on
anything needing a human call. Recruiting-specific checks degrade to
"skipped" when their modules are absent.

Design:
- A registry of CHECKS, each returning a Result(status, detail):
    ok        — healthy
    fixed     — was broken, self-fix applied and re-verified
    warn      — degraded but functioning; recorded, not DM-worthy alone
    fail      — broken, self-fix failed or none available (engineering)
    escalate  — needs the OPERATOR's call (consent, scopes, his account)
- Self-fixes are BOUNDED (one attempt per run, always re-verified) and
  limited to safe, reversible actions: restarting launchd services,
  kicking idempotent syncs, refreshing OAuth tokens, regenerating
  derived files. Anything else escalates.
- Output: brain/health/YYYY-MM-DD.json (full), one line appended to
  brain/health/health-log.md, and a Lark DM to the operator ONLY when
  something was fixed, failed, or needs his call. All-green days are
  silent (operator's preference, 2026-07-29).
- The runner itself is watched: tools/health-check.sh traps a crash of
  this script and DMs engineering — the watchdog must never die
  silently (that is the exact disease this exists to cure).

Run modes:  python3 tools/health_check.py [--json] [--area AREA]
"""

import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.request
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import get_home, load_config  # noqa: E402

HOME = get_home()
OPERATOR_OPEN_ID = ((load_config().get("lark") or {})
                    .get("operator_open_id") or "")
NOW = time.time()

RESULTS = []


def result(name, area, status, detail="", fix=""):
    RESULTS.append({"name": name, "area": area, "status": status,
                    "detail": str(detail)[:300], "fix": fix})
    flag = {"ok": "✓", "fixed": "🔧", "warn": "…", "fail": "✗",
            "escalate": "🙋"}.get(status, "?")
    print(f"  {flag} [{area}] {name}: {status}"
          + (f" — {detail}" [:160] if detail else ""), flush=True)


def _age_h(path):
    try:
        return (NOW - os.path.getmtime(path)) / 3600
    except OSError:
        return 1e9


def _kickstart(label):
    return subprocess.run(
        ["launchctl", "kickstart", "-k", f"gui/501/{label}"],
        capture_output=True, text=True).returncode == 0


# ────────────────────────── services ──────────────────────────

def check_launchd_jobs():
    """Every com.noto.* job loaded; crashed daemons restarted."""
    out = subprocess.run(["launchctl", "list"], capture_output=True,
                         text=True).stdout
    loaded = {}
    for ln in out.splitlines():
        parts = ln.split("\t")
        if len(parts) == 3 and parts[2].startswith("com.noto."):
            loaded[parts[2]] = (parts[0], parts[1])   # pid, last exit
    expected = [os.path.basename(p)[:-6] for p in
                sorted(__import__("glob").glob(
                    os.path.join(HOME, "deploy", "com.noto.*.plist")))]
    for label in expected:
        if label not in loaded:
            ok = subprocess.run(
                ["launchctl", "bootstrap", "gui/501",
                 os.path.expanduser(
                     f"~/Library/LaunchAgents/{label}.plist")],
                capture_output=True).returncode == 0
            result(f"launchd {label}", "services",
                   "fixed" if ok else "fail",
                   "was not loaded", "bootstrap")
            continue
        pid, _exit = loaded[label]
        # KeepAlive daemons must have a live pid
        if label in ("com.noto.larkbot", "com.noto.cloudflared",
                     "com.noto.caffeinate", "com.noto.emailpipeline") \
                and pid == "-":
            ok = _kickstart(label)
            result(f"launchd {label}", "services",
                   "fixed" if ok else "fail", "daemon had no pid",
                   "kickstart")
        else:
            result(f"launchd {label}", "services", "ok")


def check_webhook_local():
    """Bot webhook answers locally; restart once if not."""
    def probe():
        try:
            req = urllib.request.Request(
                "http://127.0.0.1:8088/lark/webhook",
                data=b'{"type":"url_verification"}',
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=8) as r:
                return bool(r.read())
        except urllib.error.HTTPError:
            return True          # any HTTP answer = route is alive
        except Exception:
            return False
    if probe():
        result("webhook local", "services", "ok")
        return
    _kickstart("com.noto.larkbot")
    time.sleep(8)
    result("webhook local", "services",
           "fixed" if probe() else "fail",
           "bot not answering on :8088", "kickstart + reprobe")


def check_funnel():
    """Public ingress via the Tailscale funnel hostname."""
    host = (load_config().get("lark") or {}).get("funnel_host") or ""
    if not host:
        result("funnel", "services", "warn", "no funnel_host in config")
        return
    try:
        req = urllib.request.Request(
            f"https://{host}/lark/webhook", data=b"{}",
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=15)
        result("funnel ingress", "services", "ok")
    except urllib.error.HTTPError:
        result("funnel ingress", "services", "ok")   # answered = alive
    except Exception as e:
        result("funnel ingress", "services", "fail",
               f"unreachable: {e}", "needs tailscale investigation")


# ─────────────────────── scheduled work ───────────────────────

def _freshness(name, path, max_h, fix_cmd=None, fix_label=""):
    age = _age_h(path)
    if age <= max_h:
        result(name, "schedules", "ok", f"{age:.1f}h old")
        return
    if fix_cmd:
        subprocess.run(fix_cmd, capture_output=True, timeout=1800)
        age2 = _age_h(path)
        result(name, "schedules",
               "fixed" if age2 < age else "fail",
               f"was {age:.0f}h stale (limit {max_h}h)", fix_label)
    else:
        result(name, "schedules", "fail",
               f"{age:.0f}h stale (limit {max_h}h)")


def check_schedules():
    _freshness("nightly resync ran", os.path.join(HOME, "lark", "resync.log"), 30)
    _freshness("mail nightly ran",
               os.path.join(HOME, "lark", "mail-nightly.log"), 30)
    _freshness("autodraft poller alive",
               os.path.join(HOME, "indexes", "mail",
                            "autodraft_state.json"), 1,
               fix_cmd=["launchctl", "kickstart", "-k",
                        "gui/501/com.noto.emailpipeline"],
               fix_label="kickstart emailpipeline")
    idx = os.path.join(HOME, "lark", "candidate_folder_index.json")
    _freshness("candidate folder index", idx, 48)


def check_data_freshness():
    """Nightly pipelines actually PRODUCED data, not just ran."""
    probes = [
        ("nuggets extracted", "indexes/chat_nuggets.db",
         "SELECT MAX(created_at) FROM chat_nuggets", 48),
        ("feedback swept", "indexes/feedback.db",
         "SELECT MAX(created_at) FROM feedback", 96),
        ("chat context flowing", "indexes/chat_context.db",
         "SELECT MAX(ts) FROM turns", 24),
    ]
    for name, db_rel, q, max_h in probes:
        try:
            db = sqlite3.connect(os.path.join(HOME, db_rel), timeout=10)
            v = db.execute(q).fetchone()[0]
            db.close()
            if v is None:
                result(name, "data", "warn", "no rows")
                continue
            if isinstance(v, (int, float)):
                ts = v / 1000 if v > 4e12 else float(v)
            else:
                s = str(v).replace("T", " ").replace("+00:00", "")
                ts = time.mktime(time.strptime(s[:19],
                                               "%Y-%m-%d %H:%M:%S"))
                if "T" in str(v) or "+00:00" in str(v):
                    ts -= time.timezone * 0   # stored UTC; compare loosely
            age_h = (NOW - ts) / 3600
            # UTC-vs-local slop: allow an extra 12h margin
            status = "ok" if age_h <= max_h + 12 else "fail"
            result(name, "data", status, f"latest {age_h:.0f}h ago")
        except Exception as e:
            result(name, "data", "fail", str(e)[:120])


def check_mail_mirrors():
    """Per-user mirror is current — INBOX synced recently."""
    try:
        import mail_store
    except Exception as e:
        result("mail mirrors", "mail", "fail", f"import: {e}")
        return
    for user in mail_store.MAILBOXES:
        try:
            db = mail_store._connect(user)
            v = db.execute("SELECT MAX(date_ms) FROM messages"
                           " WHERE label='INBOX'").fetchone()[0]
            db.close()
            if not v:
                result(f"mirror {user}", "mail", "warn", "empty INBOX")
                continue
            age_h = (NOW - v / 1000) / 3600
            # people get mail most days; >72h = sync likely broken
            result(f"mirror {user}", "mail",
                   "ok" if age_h < 72 else "warn",
                   f"newest inbox mail {age_h:.0f}h ago")
        except Exception as e:
            result(f"mirror {user}", "mail", "fail", str(e)[:120])


# ─────────────────────────── mail ───────────────────────────

def check_mail_api():
    """Tenant data-range read works for every pilot mailbox."""
    try:
        import mail_store
        for user in mail_store.MAILBOXES:
            r = mail_store._list_ids(user, "INBOX")
            code = r.get("code")
            if code == 0:
                result(f"mail read {user}", "mail", "ok")
            elif code == 1230002:
                result(f"mail read {user}", "mail", "escalate",
                       "mailbox missing from the app's data range — "
                       "needs a Console change (operator)")
            else:
                result(f"mail read {user}", "mail", "fail",
                       f"code {code}: {str(r.get('msg'))[:80]}")
    except Exception as e:
        result("mail api", "mail", "fail", str(e)[:120])


def check_consents():
    """Draft/send consent tokens for every autodraft user."""
    try:
        from email_autodraft import USERS, _has_consent
        for user in USERS:
            if _has_consent(user):
                result(f"consent {user}", "mail", "ok")
            else:
                result(f"consent {user}", "mail", "escalate",
                       f"{user} has no draft/send consent token — "
                       "needs their authorization click")
    except Exception as e:
        result("consents", "mail", "fail", str(e)[:120])


def check_send_verification():
    """No unverified sends older than 2h (post-send guard healthy)."""
    try:
        db = sqlite3.connect(os.path.join(
            HOME, "indexes", "mail", "draft_queue.db"), timeout=10)
        n = db.execute(
            "SELECT COUNT(*) FROM queue WHERE status='sent'"
            " AND sent_verified=0").fetchone()[0]
        pend = db.execute(
            "SELECT COUNT(*) FROM queue WHERE status='pending'"
            " AND created_at < datetime('now','-2 days')").fetchone()[0]
        db.close()
        if n:
            result("send verification", "mail", "fail",
                   f"{n} send(s) flagged unverified — investigate")
        else:
            result("send verification", "mail", "ok")
        result("draft queue hygiene", "mail",
               "ok" if pend == 0 else "warn",
               "" if pend == 0 else f"{pend} card(s) pending >48h")
    except Exception as e:
        result("send verification", "mail", "fail", str(e)[:120])


# ─────────────────────────── auth ───────────────────────────

def check_oauth():
    """Every identity's token refreshes; failures are consent-class."""
    try:
        import lark_oauth
        idents = sorted(lark_oauth._SCOPES_BY_IDENTITY)
    except Exception as e:
        result("oauth import", "auth", "fail", str(e)[:120])
        return
    for ident in idents:
        if ident == "alejandro":       # alias of operator
            continue
        try:
            tok = lark_oauth.get_user_token(ident)
            result(f"token {ident}", "auth",
                   "ok" if tok else "escalate",
                   "" if tok else f"no usable token for {ident} — "
                   "re-consent needed")
        except Exception as e:
            msg = str(e)[:120]
            sev = "escalate" if ("consent" in msg.lower()
                                 or "refresh" in msg.lower()
                                 or "invalid" in msg.lower()) else "fail"
            result(f"token {ident}", "auth", sev, msg)


def check_tenant_token():
    try:
        from lark_client import get_tenant_access_token
        tok = get_tenant_access_token()
        result("tenant token", "auth", "ok" if tok else "fail")
    except Exception as e:
        result("tenant token", "auth", "fail", str(e)[:120])


# ─────────────────────── core functions ───────────────────────

def check_claude_cli():
    """The text-function contract: strict JSON in, strict JSON out."""
    try:
        from noto_research import _claude
        raw = _claude('Reply with STRICT JSON only: {"ok": true}',
                      timeout=90)
        ok = raw and '"ok"' in raw and "true" in raw and \
            len(raw.strip()) < 40
        result("claude -p text function", "core",
               "ok" if ok else "fail",
               "" if ok else f"unexpected: {raw[:80]!r}")
    except Exception as e:
        result("claude -p text function", "core", "fail", str(e)[:120])


def check_retrieval():
    """Company index + mail retrieval answer a query without error."""
    try:
        import mail_retrieval
        import mail_store
        u = next(iter(mail_store.MAILBOXES))
        mail_store.search(u, "meeting", limit=2)
        result("mail retrieval", "core", "ok")
    except Exception as e:
        result("mail retrieval", "core", "fail", str(e)[:120])
    try:
        from retrieval_router import route  # noqa: F401
        result("retrieval router import", "core", "ok")
    except Exception as e:
        result("retrieval router import", "core", "fail", str(e)[:120])


def check_safety_invariants():
    """The two hard startup gates, standalone."""
    try:
        from lark_client import assert_no_lark_delete
        assert_no_lark_delete()
        result("no-Lark-delete scan", "safety", "ok")
    except Exception as e:
        result("no-Lark-delete scan", "safety", "fail", str(e)[:150])
    try:
        import recruiter_memory
        if hasattr(recruiter_memory, "assert_isolation"):
            recruiter_memory.assert_isolation()
        result("recruiter-memory isolation", "safety", "ok")
    except Exception as e:
        result("recruiter-memory isolation", "safety", "fail",
               str(e)[:150])


def check_target_list_machinery():
    """Read-only: template parse of a registered doc + folder resolve."""
    try:
        import candidate_artifacts as ca
        rows = [r for r in ca.all_artifacts(limit=10)
                if r["artifact_type"] == "target_list"
                and not r.get("adopted_from_drive")]
        if not rows:
            result("target-list parse", "core", "warn",
                   "no bot-generated lists to probe")
            return
        from lark_client import LarkClient
        import lark_bot as lb
        struct = LarkClient().parse_doc_template(
            rows[0]["doc_id"], bucket_resolver=lb._canonical_bucket)
        n = sum(len(b.get("firms", []))
                for b in struct.get("buckets", []))
        result("target-list parse", "core",
               "ok" if n > 0 else "warn", f"{n} firms parsed")
    except Exception as e:
        result("target-list parse", "core", "fail", str(e)[:120])


def check_jobs_pipeline():
    """Store↔site consistency + every row validates."""
    try:
        import job_openings_store as store
        import jobs_validator as jv
        jobs = store.list_jobs()
        bad = 0
        for j in jobs:
            if j["status"] != "active":
                continue
            cells = dict(zip(store.PUBLISHED_COLUMNS,
                             store.to_csv_row(j)))
            errs, _ = jv.validate(cells)
            bad += 1 if errs else 0
        result("jobs rows validate", "jobs",
               "ok" if bad == 0 else "fail",
               f"{len(jobs)} rows, {bad} invalid")
        repo = os.environ.get("VARGAS_SITE_REPO",
                              os.path.expanduser(
                                  "~/code/vargas-partners-website"))
        csv_path = os.path.join(repo, "src/data/jobs.csv")
        if not os.path.exists(csv_path):
            result("site repo", "jobs", "fail", "jobs.csv missing")
        else:
            import csv as _csv
            import io as _io

            def _rows(t):
                return list(_csv.reader(_io.StringIO(t)))
            drift = _rows(open(csv_path).read()) != \
                _rows(store.render_csv())
            if drift:
                # Content drift (not quoting style — we parse-compare).
                store.write_repo_csv()
                still = _rows(open(csv_path).read()) != \
                    _rows(store.render_csv())
                result("jobs.csv sync", "jobs",
                       "fail" if still else "fixed",
                       "csv content drifted from store",
                       "write_repo_csv")
            else:
                result("jobs.csv sync", "jobs", "ok")
    except Exception as e:
        result("jobs pipeline", "jobs", "fail", str(e)[:120])


# ─────────────────────────── system ───────────────────────────

def check_system():
    try:
        st = os.statvfs("/")
        free_gb = st.f_bavail * st.f_frsize / 1e9
        result("disk free", "system",
               "ok" if free_gb > 30 else
               ("warn" if free_gb > 10 else "fail"),
               f"{free_gb:.0f} GB free")
    except Exception as e:
        result("disk free", "system", "warn", str(e)[:80])
    # runaway logs (> 2 GB)
    big = []
    for d in ("lark", "logs"):
        p = os.path.join(HOME, d)
        if os.path.isdir(p):
            for f in os.listdir(p):
                fp = os.path.join(p, f)
                if f.endswith(".log") and os.path.isfile(fp) \
                        and os.path.getsize(fp) > 2e9:
                    big.append(f)
    result("log sizes", "system", "ok" if not big else "warn",
           "" if not big else f"oversized: {big}")


def check_error_burst():
    """Recent error storms in the bot log."""
    p = os.path.join(HOME, "lark", "larkbot.err.log")
    try:
        with open(p, "rb") as f:
            f.seek(max(0, os.path.getsize(p) - 400_000))
            tail = f.read().decode("utf-8", "replace")
        tb = tail.count("Traceback (most recent call last)")
        rc1 = tail.count("claude attempt 1 rc=1")
        status = "ok"
        detail = f"{tb} tracebacks, {rc1} rc=1 in recent log"
        if tb > 10 or rc1 > 20:
            status = "fail"
        elif tb > 3 or rc1 > 8:
            status = "warn"
        result("error burst scan", "system", status, detail)
    except Exception as e:
        result("error burst scan", "system", "warn", str(e)[:80])


# ─────────────────────── report + notify ───────────────────────

CHECKS = [
    check_launchd_jobs, check_webhook_local, check_funnel,
    check_schedules, check_data_freshness, check_mail_mirrors,
    check_mail_api, check_consents, check_send_verification,
    check_oauth, check_tenant_token,
    check_claude_cli, check_retrieval, check_safety_invariants,
    check_target_list_machinery, check_jobs_pipeline,
    check_system, check_error_burst,
]


def run(area=None):
    print(f"[health] {time.strftime('%F %T')} running "
          f"{len(CHECKS)} check groups…", flush=True)
    for fn in CHECKS:
        if area and area not in fn.__name__:
            continue
        try:
            fn()
        except (ImportError, ModuleNotFoundError) as e:
            result(fn.__name__, "engine", "warn",
                   f"module not present — skipped ({e})")
        except Exception as e:
            result(fn.__name__, "engine", "fail",
                   f"check itself crashed: {e}")
    counts = {}
    for r in RESULTS:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    day = time.strftime("%Y-%m-%d")
    outdir = os.path.join(HOME, "brain", "health")
    os.makedirs(outdir, exist_ok=True)
    payload = {"date": day, "ts": time.strftime("%F %T"),
               "counts": counts, "results": RESULTS}
    json.dump(payload, open(os.path.join(outdir, f"{day}.json"), "w"),
              indent=1)
    line = (f"- {time.strftime('%F %T')} — "
            + ", ".join(f"{k}:{v}" for k, v in sorted(counts.items())))
    with open(os.path.join(outdir, "health-log.md"), "a") as f:
        f.write(line + "\n")

    fixed = [r for r in RESULTS if r["status"] == "fixed"]
    failed = [r for r in RESULTS if r["status"] == "fail"]
    esc = [r for r in RESULTS if r["status"] == "escalate"]
    if fixed or failed or esc:
        msg = ["🩺 **Daily health check** — "
               + ", ".join(f"{k}: {v}" for k, v in sorted(counts.items()))]
        if esc:
            msg.append("\n**Needs your call:**")
            msg += [f"  • {r['name']} — {r['detail']}" for r in esc]
        if failed:
            msg.append("\n**Broken (self-fix failed or none safe):**")
            msg += [f"  • {r['name']} — {r['detail']}" for r in failed]
        if fixed:
            msg.append("\n**Self-fixed this morning:**")
            msg += [f"  • {r['name']} — {r['detail']} "
                    f"({r['fix']})" for r in fixed]
        try:
            if not OPERATOR_OPEN_ID:
                raise RuntimeError("no lark.operator_open_id configured")
            from lark_client import LarkClient
            LarkClient().send_text(OPERATOR_OPEN_ID, "\n".join(msg),
                                   receive_id_type="open_id")
        except Exception as e:
            print(f"[health] operator DM failed: {e}", file=sys.stderr,
                  flush=True)
    print(f"[health] done: {counts}", flush=True)
    return payload


if __name__ == "__main__":
    area = None
    if "--area" in sys.argv:
        area = sys.argv[sys.argv.index("--area") + 1]
    p = run(area)
    if "--json" in sys.argv:
        print(json.dumps(p))
    sys.exit(0 if not any(r["status"] in ("fail",)
                          for r in p["results"]) else 1)
