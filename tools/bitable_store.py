#!/usr/bin/env python3
"""
Generic Bitable row helpers — Foundation C of the H2 roadmap.

One module owns row-level access to any Lark Base the operator points
us at: the Candidate Pipeline today, the expenses Base (F2) and the F7
prospect rows tomorrow. Extracted/generalized from pipeline_apply's
direct-HTTP fallbacks (2026-07): same operator user_token + Bitable v1
endpoints, but the search key-field is a parameter instead of a
hardcoded "Name".

Surface (all best-effort, return dicts, never raise past the caller):
  search_rows(app, table, field, value)   -> {"items": [...], ...}
  list_rows(app, table, page_size)        -> [records]  (via lark_client)
  create_row(app, table, fields)          -> raw API response
  update_row(app, table, record_id, fields) -> raw API response
  upsert_row(app, table, key_field, key_value, fields, create_extra)
      -> {"ok", "action": "updated"|"created", "record_id", ...}

Data-safety: search (POST …/records/search), create (POST …/records)
and update (PUT …/records/{id}) only — no record deletion exists here,
by design (Lark Data Safety rule; lark_client.assert_no_lark_delete
scans this file too).
"""

import json
import sys
from typing import Any, Dict, List, Optional

sys.path.insert(0, __file__.rsplit("/", 1)[0])


def _base_url() -> str:
    from config import load_config
    return (load_config().get("lark", {})
            .get("base_url", "https://open.larksuite.com")).rstrip("/")


def _http(method: str, path: str,
          body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Direct HTTP against the Bitable v1 API with the operator
    user_token (Bases are shared with the Noah user, not the app —
    see the 'Lark bots can't be shared' rule)."""
    import urllib.request
    from lark_oauth import get_user_token
    req = urllib.request.Request(
        f"{_base_url()}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"Authorization": f"Bearer {get_user_token('operator')}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def search_rows(app_token: str, table_id: str,
                field: str, value: str,
                page_size: int = 5) -> Dict[str, Any]:
    """Rows where `field` is exactly `value`. Returns the API's `data`
    dict ({"items": [...], "has_more": ...}); {} on error."""
    try:
        d = _http("POST",
                  f"/open-apis/bitable/v1/apps/{app_token}/tables/"
                  f"{table_id}/records/search",
                  {"filter": {"conjunction": "and",
                              "conditions": [{"field_name": field,
                                              "operator": "is",
                                              "value": [value]}]},
                   "page_size": page_size})
        return d.get("data") or {}
    except Exception as e:
        print(f"[bitable_store] search failed ({field}={value!r}): "
              f"{str(e)[:120]}", file=sys.stderr, flush=True)
        return {}


def list_rows(app_token: str, table_id: str) -> List[Dict[str, Any]]:
    """All records in a table. SDK first; falls back to direct HTTP
    with the operator user token — required for Bases living on
    another tenant domain (e.g. the Reimbursement Base), where the
    SDK's app credentials get 91403 Forbidden but the user token,
    which was granted access as a person, works."""
    try:
        from lark_client import LarkClient
        return LarkClient().bitable_list_records(
            app_token, table_id) or []
    except Exception:
        pass
    out: List[Dict[str, Any]] = []
    page = ""
    try:
        while True:
            path = (f"/open-apis/bitable/v1/apps/{app_token}/tables/"
                    f"{table_id}/records?page_size=100"
                    + (f"&page_token={page}" if page else ""))
            d = _http("GET", path).get("data") or {}
            out.extend(d.get("items") or [])
            page = d.get("page_token") or ""
            if not (d.get("has_more") and page):
                break
    except Exception as e:
        print(f"[bitable_store] list failed: {str(e)[:120]}",
              file=sys.stderr, flush=True)
    return out


def create_row(app_token: str, table_id: str,
               fields: Dict[str, Any]) -> Dict[str, Any]:
    return _http("POST",
                 f"/open-apis/bitable/v1/apps/{app_token}/tables/"
                 f"{table_id}/records",
                 {"fields": fields})


def update_row(app_token: str, table_id: str, record_id: str,
               fields: Dict[str, Any]) -> Dict[str, Any]:
    try:
        from lark_client import LarkClient
        client = LarkClient()
        if hasattr(client, "bitable_update_record"):
            return client.bitable_update_record(
                app_token=app_token, table_id=table_id,
                record_id=record_id, fields=fields) or {}
    except Exception:
        pass  # fall through to direct HTTP
    return _http("PUT",
                 f"/open-apis/bitable/v1/apps/{app_token}/tables/"
                 f"{table_id}/records/{record_id}",
                 {"fields": fields})


def created_record_id(create_response: Dict[str, Any]) -> str:
    """Pull the record_id out of a create_row response (two shapes seen
    in the wild)."""
    return (((create_response.get("data") or {}).get("record") or {})
            .get("record_id")
            or (create_response.get("record") or {}).get("record_id")
            or "")


def upsert_row(app_token: str, table_id: str,
               key_field: str, key_value: str,
               fields: Dict[str, Any],
               create_extra: Optional[Dict[str, Any]] = None,
               ) -> Dict[str, Any]:
    """Update the first row where key_field == key_value, else create
    one. Update writes `fields` only; create writes
    {key_field: key_value, **fields, **create_extra}. Callers that must
    gate creation (e.g. pipeline's folder-existence rule) search first
    and decide themselves."""
    try:
        items = (search_rows(app_token, table_id, key_field, key_value)
                 .get("items") or [])
        if items:
            rec_id = items[0].get("record_id")
            update_row(app_token, table_id, rec_id, fields)
            return {"ok": True, "action": "updated", "record_id": rec_id}
        new_fields = {key_field: key_value, **fields,
                      **(create_extra or {})}
        created = create_row(app_token, table_id, new_fields)
        return {"ok": True, "action": "created",
                "record_id": created_record_id(created)}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}
