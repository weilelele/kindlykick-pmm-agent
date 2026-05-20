from __future__ import annotations

from datetime import date
from supabase import create_client, Client
from config import settings

_client: Client = create_client(settings.supabase_url, settings.supabase_service_key)


# ── Members ──────────────────────────────────────────────────────

def get_members() -> list[dict]:
    return _client.table("members").select("*").order("name").execute().data


def get_member_by_open_id(open_id: str) -> dict | None:
    rows = _client.table("members").select("*").eq("feishu_user_id", open_id).execute().data
    return rows[0] if rows else None


def create_member(name: str, open_id: str, is_owner: bool = False) -> dict:
    return _client.table("members").insert({
        "name": name,
        "feishu_user_id": open_id,
        "is_owner": is_owner,
    }).execute().data[0]


def update_member_bio(open_id: str, bio: str) -> None:
    _client.table("members").update({"bio": bio}).eq("feishu_user_id", open_id).execute()


def get_unassigned_tasks() -> list[dict]:
    """Return active tasks with no assignee."""
    return (
        _client.table("tasks")
        .select("*")
        .is_("assignee_id", "null")
        .neq("status", "done")
        .order("created_at")
        .execute()
        .data
    )


def find_tasks_mentioning_name(name: str) -> list[dict]:
    """Find active unassigned tasks whose title or description mentions the given name."""
    unassigned = get_unassigned_tasks()
    return [
        t for t in unassigned
        if name in (t.get("title") or "") or name in (t.get("description") or "")
    ]


# ── Tasks ─────────────────────────────────────────────────────────

def get_active_tasks() -> list[dict]:
    return (
        _client.table("tasks")
        .select("*")
        .neq("status", "done")
        .order("priority", desc=True)
        .order("created_at")
        .execute()
        .data
    )


def get_tasks_by_assignee(assignee_name: str) -> list[dict]:
    return (
        _client.table("tasks")
        .select("*")
        .eq("assignee_name", assignee_name)
        .neq("status", "done")
        .execute()
        .data
    )


def create_task(task: dict) -> dict:
    return _client.table("tasks").insert(task).execute().data[0]


def create_tasks_bulk(tasks: list[dict]) -> list[dict]:
    if not tasks:
        return []
    return _client.table("tasks").insert(tasks).execute().data


def update_task(task_id: str, updates: dict) -> None:
    updates = {**updates, "updated_at": "now()"}
    _client.table("tasks").update(updates).eq("id", task_id).execute()


def find_task_by_id_prefix(prefix: str) -> dict | None:
    rows = _client.table("tasks").select("*").like("id", f"{prefix}%").execute().data
    return rows[0] if rows else None


# ── Progress logs ─────────────────────────────────────────────────

def add_progress_log(
    member_name: str,
    content: str,
    source: str,
    task_id: str | None = None,
    member_id: str | None = None,
) -> dict:
    return _client.table("progress_logs").insert({
        "task_id": task_id,
        "member_id": member_id,
        "member_name": member_name,
        "content": content,
        "source": source,
    }).execute().data[0]


def get_today_progress() -> list[dict]:
    today = date.today().isoformat()
    return (
        _client.table("progress_logs")
        .select("*")
        .gte("created_at", f"{today}T00:00:00+00:00")
        .order("created_at")
        .execute()
        .data
    )


# ── Meetings ──────────────────────────────────────────────────────

def save_meeting(title: str, raw_content: str, summary: str, tasks_count: int) -> dict:
    return _client.table("meetings").insert({
        "title": title,
        "raw_content": raw_content,
        "summary": summary,
        "tasks_extracted": tasks_count,
    }).execute().data[0]


def meeting_already_processed(doc_id: str) -> bool:
    """Return True if a meeting with this doc_id (stored in raw_content prefix) was already processed."""
    rows = (
        _client.table("meetings")
        .select("id")
        .like("raw_content", f"[doc:{doc_id}]%")
        .execute()
        .data
    )
    return len(rows) > 0
