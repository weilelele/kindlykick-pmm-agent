"""
Feishu Bitable client — task CRUD.
Replaces the Supabase tasks table.

Field layout (matching the 任务看板 table):
  任务标题  (Text, primary)
  负责人    (Person)
  状态      (Single select: 待开始 / 进行中 / 受阻 / 已完成)
  优先级    (Single select: 🔴 高 / 🟡 普通 / 🟢 低)
  截止日期  (Date, ms timestamp)
  备注      (Text)
"""

from __future__ import annotations

import httpx
from datetime import datetime, timezone

from config import settings

APP_TOKEN = "TXqAbSLzcazyvasaHHsc7lGLndf"
TABLE_ID  = "tblmnck45Ae44Dqa"
_BASE     = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{APP_TOKEN}/tables/{TABLE_ID}"


# ── Token helper ──────────────────────────────────────────────────

async def _tok() -> str:
    from feishu_client import _get_app_token
    return await _get_app_token()


# ── Date conversion ───────────────────────────────────────────────

def _to_ms(date_str: str | None) -> int | None:
    if not date_str:
        return None
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except ValueError:
        return None


def _from_ms(ms: int | float | None) -> str | None:
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


# ── Record normaliser ─────────────────────────────────────────────

def _normalize(raw: dict) -> dict:
    """Convert raw Bitable API record → clean task dict."""
    fields = raw.get("fields", {})

    # Person field → [{"id": open_id, "name": "...", ...}]
    persons = fields.get("负责人") or []
    assignee_name     = persons[0].get("name")    if persons else None
    assignee_open_id  = persons[0].get("id")      if persons else None

    # Single-select fields come back as {"text": "...", "id": "opt..."}
    def _sel(val):
        if isinstance(val, dict):
            return val.get("text") or val.get("value")
        return val

    return {
        "record_id":        raw["record_id"],
        "title":            fields.get("任务标题", ""),
        "assignee_name":    assignee_name,
        "assignee_open_id": assignee_open_id,
        "status":           _sel(fields.get("状态"))    or "待开始",
        "priority":         _sel(fields.get("优先级"))  or "🟡 普通",
        "due_date":         _from_ms(fields.get("截止日期")),
        "notes":            fields.get("备注") or "",
    }


# ── Build fields dict for write operations ────────────────────────

def _build_fields(task: dict) -> dict:
    fields: dict = {}

    if task.get("title"):
        fields["任务标题"] = task["title"]

    if task.get("assignee_open_id"):
        fields["负责人"] = [{"id": task["assignee_open_id"]}]

    if task.get("status"):
        fields["状态"] = task["status"]

    if task.get("priority"):
        fields["优先级"] = task["priority"]

    due_ms = _to_ms(task.get("due_date"))
    if due_ms:
        fields["截止日期"] = due_ms

    if task.get("notes"):
        fields["备注"] = task["notes"]

    return fields


# ── CRUD ──────────────────────────────────────────────────────────

async def list_records() -> list[dict]:
    """Return all task records (active + done)."""
    tok = await _tok()
    items: list[dict] = []
    page_token: str | None = None

    async with httpx.AsyncClient(timeout=15) as client:
        while True:
            params: dict = {"page_size": 100}
            if page_token:
                params["page_token"] = page_token
            r = await client.get(
                f"{_BASE}/records",
                headers={"Authorization": f"Bearer {tok}"},
                params=params,
            )
            data = r.json().get("data", {})
            items.extend(data.get("items") or [])
            if not data.get("has_more"):
                break
            page_token = data.get("page_token")

    return [_normalize(rec) for rec in items]


async def list_active_records() -> list[dict]:
    """Return tasks where status != 已完成."""
    all_recs = await list_records()
    return [t for t in all_recs if t["status"] != "已完成"]


async def create_record(task: dict) -> dict:
    """Create a new record. task keys: title, assignee_open_id, status, priority, due_date, notes."""
    tok = await _tok()
    fields = _build_fields(task)
    if "状态" not in fields:
        fields["状态"] = "待开始"
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            f"{_BASE}/records",
            headers={"Authorization": f"Bearer {tok}"},
            json={"fields": fields},
        )
    return _normalize(r.json().get("data", {}).get("record", {}))


async def update_record(record_id: str, updates: dict) -> None:
    """Update a single record. updates keys same as task dict (minus title)."""
    tok = await _tok()
    fields = _build_fields(updates)
    if not fields:
        return
    async with httpx.AsyncClient(timeout=15) as client:
        await client.put(
            f"{_BASE}/records/{record_id}",
            headers={"Authorization": f"Bearer {tok}"},
            json={"fields": fields},
        )


async def delete_record(record_id: str) -> None:
    tok = await _tok()
    async with httpx.AsyncClient(timeout=15) as client:
        await client.delete(
            f"{_BASE}/records/{record_id}",
            headers={"Authorization": f"Bearer {tok}"},
        )
