"""
Message routing and tool executor.

All messages go through a single Claude tool_use call.
Claude decides which tools to call; this module executes them.

Tools:
  create_task    → bitable_client.create_record
  update_tasks   → filter records, bitable_client.update_record (batched)
  delete_tasks   → filter records, bitable_client.delete_record (batched)
  log_progress   → database.add_progress_log + optional status update
  reply          → send the final message
"""

from __future__ import annotations

import base64
import json
import logging
import re
from typing import Callable, Awaitable

import database as db
import bitable_client as bc
import ai_client as ai
import feishu_client as fc
from config import settings

log = logging.getLogger(__name__)

_DOC_URL_RE = re.compile(
    r"https?://[a-zA-Z0-9\-]+\.(?:feishu\.cn|larkoffice\.com)/(?:docx|docs|wiki)/([A-Za-z0-9]+)"
)

SendFn = Callable[[str], Awaitable[None]]


# ── Top-level dispatcher ──────────────────────────────────────────

async def handle_message(event_body: dict) -> None:
    event  = event_body.get("event", {})
    msg    = event.get("message", {})
    sender = event.get("sender", {})

    chat_type      = msg.get("chat_type")
    chat_id        = msg.get("chat_id")
    message_id     = msg.get("message_id", "")
    message_type   = msg.get("message_type", "")
    sender_open_id = sender.get("sender_id", {}).get("open_id", "")

    if not sender_open_id or not chat_id:
        return

    member      = await _ensure_member(sender_open_id)
    sender_name = member["name"] if member else "未知成员"

    if chat_type == "p2p":
        send: SendFn = lambda text: fc.send_to_user(sender_open_id, text)
        if sender_open_id == settings.owner_open_id:
            await _handle_owner_message(message_type, msg, sender_open_id, message_id, send)
        else:
            await _handle_member_private(sender_open_id, sender_name, message_type, msg, message_id, send)

    elif chat_type == "group":
        mentions = msg.get("mentions", [])
        bot_mentioned = any(
            m.get("id", {}).get("open_id") == settings.bot_open_id
            for m in mentions
        )
        if bot_mentioned:
            send = lambda text: fc.send_to_chat(chat_id, text)
            await _handle_group_mention(sender_open_id, sender_name, message_type, msg, chat_id, message_id, send)


# ── Auto-register unknown members ────────────────────────────────

async def _ensure_member(open_id: str) -> dict | None:
    member = db.get_member_by_open_id(open_id)
    if member:
        return member
    try:
        name = await fc.get_user_name(open_id)
        if name:
            member = db.create_member(name, open_id, open_id == settings.owner_open_id)
            log.info("Auto-registered: %s (%s)", name, open_id)
            return member
    except Exception as e:
        log.warning("Could not auto-register %s: %s", open_id, e)
    return None


# ── Content extraction ────────────────────────────────────────────

async def _extract_text(message_type: str, msg: dict, send: SendFn, message_id: str) -> str | None:
    content_raw = msg.get("content", "{}")

    if message_type == "text":
        raw = json.loads(content_raw).get("text", "").strip()
        return re.sub(r"<at user_id=\"[^\"]+\">.*?</at>", "", raw).strip()

    elif message_type == "post":
        doc   = json.loads(content_raw)
        parts = []
        for lang in doc.values():
            if lang.get("title"):
                parts.append(lang["title"])
            for block in lang.get("content", []):
                for elem in block:
                    if elem.get("tag") == "text":
                        parts.append(elem.get("text", ""))
        return "\n".join(parts).strip()

    elif message_type == "file":
        parsed   = json.loads(content_raw)
        file_key = parsed.get("file_key", "")
        file_name= parsed.get("file_name", "")
        if not file_key:
            return None
        await send(f"⏳ 正在读取文件「{file_name}」…")
        data = await fc.download_file(message_id, file_key)
        if not data:
            await send("❌ 文件下载失败，请检查 bot 是否有 `im:resource` 权限。")
            return None
        for enc in ("utf-8", "gbk"):
            try:
                return data.decode(enc)
            except UnicodeDecodeError:
                continue
        await send("❌ 暂不支持此文件格式，请将内容复制为文本后发送。")
        return None

    elif message_type == "image":
        parsed    = json.loads(content_raw)
        image_key = parsed.get("image_key", "")
        if not image_key:
            return None
        await send("⏳ 正在识别图片内容…")
        data = await fc.download_image(message_id, image_key)
        if not data:
            await send("❌ 图片下载失败。")
            return None
        return await ai.extract_text_from_image(base64.standard_b64encode(data).decode())

    return None


async def _resolve_doc(text: str, send: SendFn) -> tuple[str, str | None]:
    """If text has a Feishu doc URL, fetch its content. Returns (final_text, doc_id_or_None)."""
    match = _DOC_URL_RE.search(text)
    if not match:
        return text, None

    doc_id = match.group(1)
    if db.meeting_already_processed(doc_id):
        await send(f"ℹ️ 该文档（{doc_id[:8]}…）已处理过。")
        return "__already_processed__", doc_id

    await send("⏳ 正在读取飞书文档…")
    content = await fc.get_feishu_doc_content(doc_id)
    if content is None:
        await send("❌ 无法读取该飞书文档，请确认文档权限及 bot 是否有 `docx:document:readonly`。")
        return "__error__", doc_id

    return f"[doc:{doc_id}]\n{content}", doc_id


# ── Core: execute Claude tool calls ──────────────────────────────

async def _handle_content(
    text: str,
    sender_name: str,
    sender_open_id: str | None,
    send: SendFn,
    source: str,
    at_prefix: str = "",
) -> None:
    tasks   = await bc.list_active_records()
    members = db.get_members()

    tool_calls = await ai.process_message(text, sender_name, tasks, members)
    log.info("Tool calls from Claude: %s", [c.name for c in tool_calls])

    reply_text: str | None = None
    tasks_modified = False

    for call in tool_calls:
        name = call.name
        inp  = call.input

        if name == "create_task":
            title = inp["title"].strip()
            # Skip if a task with the same title already exists
            if any(t.get("title", "").strip() == title for t in tasks):
                log.info("Skipping duplicate task: %s", title)
                continue
            assignee_oid = _resolve_open_id(inp.get("assignee_name"), members)
            new_rec = await bc.create_record({
                "title":            title,
                "assignee_open_id": assignee_oid,
                "status":           inp.get("status", "待开始"),
                "priority":         inp.get("priority", "🟡 普通"),
                "due_date":         inp.get("due_date"),
                "notes":            inp.get("notes"),
            })
            # Add to local cache so subsequent create_task calls in same batch also dedup
            tasks.append(new_rec)
            log.info("Created task: %s → %s", title, inp.get("assignee_name"))
            tasks_modified = True

        elif name == "update_tasks":
            targets = _filter_records(tasks, inp.get("filter", {}))
            updates = dict(inp.get("updates", {}))
            if "assignee_name" in updates:
                updates["assignee_open_id"] = _resolve_open_id(updates.pop("assignee_name"), members)
            for rec in targets:
                await bc.update_record(rec["record_id"], updates)
            log.info("Updated %d records with %s", len(targets), updates)
            tasks_modified = True

        elif name == "delete_tasks":
            targets = _filter_records(tasks, inp.get("filter", {}))
            for rec in targets:
                await bc.delete_record(rec["record_id"])
            log.info("Deleted %d records", len(targets))
            tasks_modified = True

        elif name == "log_progress":
            member = db.get_member_by_open_id(sender_open_id) if sender_open_id else None
            # Find related task to update status
            task_title = inp.get("task_title")
            new_status  = inp.get("new_status")
            if task_title and new_status:
                matched = [t for t in tasks if task_title in t["title"] or t["title"] in task_title]
                for t in matched:
                    await bc.update_record(t["record_id"], {"status": new_status})
            db.add_progress_log(
                member_name=inp["member_name"],
                content=inp["content"],
                source=source,
                task_id=None,
                member_id=member["id"] if member else None,
            )
            reply_text = inp["reply"]

        elif name == "reply":
            reply_text = inp["message"]

    if reply_text:
        await send(f"{at_prefix}{reply_text}")
    elif not any(c.name == "reply" for c in tool_calls):
        # Safety fallback — Claude forgot to call reply
        await send(f"{at_prefix}✅ 操作完成。")

    # After any write operation, fetch fresh tasks and send updated list
    if tasks_modified:
        fresh = await bc.list_active_records()
        await send(_format_task_list(fresh))


# ── Helpers ───────────────────────────────────────────────────────

def _resolve_open_id(name: str | None, members: list[dict]) -> str | None:
    if not name:
        return None
    for m in members:
        if name in m["name"] or m["name"] in name:
            return m["feishu_user_id"]
    return None


def _filter_records(tasks: list[dict], filter_: dict) -> list[dict]:
    result = list(tasks)

    if filter_.get("all_unassigned"):
        result = [t for t in result if not t.get("assignee_name")]

    if filter_.get("assignee_name"):
        name = filter_["assignee_name"]
        result = [t for t in result if name in (t.get("assignee_name") or "")]

    if filter_.get("status"):
        result = [t for t in result if t.get("status") == filter_["status"]]

    if filter_.get("titles"):
        matched = []
        for t in result:
            tt = t.get("title", "")
            if any(ft in tt or tt in ft for ft in filter_["titles"]):
                matched.append(t)
        result = matched

    return result


def _format_task_list(tasks: list[dict]) -> str:
    if not tasks:
        return "📋 当前任务列表为空。"
    status_emoji = {"待开始": "⏳", "进行中": "🔄", "受阻": "🚫", "已完成": "✅"}
    prio_emoji   = {"🔴 高": "🔴", "🟡 普通": "🟡", "🟢 低": "🟢"}
    by_assignee: dict[str, list] = {}
    for t in tasks:
        by_assignee.setdefault(t.get("assignee_name") or "待分配", []).append(t)
    lines = ["📋 当前任务列表：\n"]
    for name, ts in by_assignee.items():
        lines.append(f"【{name}】")
        for t in ts:
            se  = status_emoji.get(t["status"], "📌")
            pe  = prio_emoji.get(t.get("priority", "🟡 普通"), "🟡")
            due = f"  截止 {t['due_date']}" if t.get("due_date") else ""
            lines.append(f"  {se}{pe} {t['title']}{due}")
    return "\n".join(lines)


# ── Owner private chat ────────────────────────────────────────────

async def _handle_owner_message(
    message_type: str, msg: dict, open_id: str, message_id: str, send: SendFn
) -> None:
    content_raw = msg.get("content", "{}")

    if message_type == "text":
        raw = json.loads(content_raw).get("text", "").strip()
        if raw.startswith("/"):
            await _handle_command(raw, send)
            return

    await send("👀 收到，处理中…")

    text = await _extract_text(message_type, msg, send, message_id)
    if text is None:
        await send("暂不支持此消息类型。请发送：文字、飞书文档链接、文本文件或图片。")
        return

    text, doc_id = await _resolve_doc(text, send)
    if text in ("__already_processed__", "__error__"):
        return

    # Tag doc so meeting dedup works
    if doc_id:
        db.save_meeting(f"[doc:{doc_id}]", text, "", 0)

    await _handle_content(text, "管理员", open_id, send, "private_chat")


async def _handle_command(text: str, send: SendFn) -> None:
    parts = text.strip().split()
    cmd   = parts[0].lower()

    if cmd in ("/添加成员", "/add_member"):
        if len(parts) < 3:
            await send("用法：/添加成员 姓名 open_id")
            return
        try:
            db.create_member(parts[1], parts[2])
            await send(f"✅ 已添加成员：{parts[1]}")
        except Exception as e:
            await send(f"❌ 添加失败：{e}")
        return

    if cmd in ("/状态", "/status"):
        tasks = await bc.list_active_records()
        await send(_format_task_list(tasks))
        return

    if cmd in ("/成员", "/members"):
        members = db.get_members()
        if not members:
            await send("👥 尚未添加任何成员")
            return
        lines = ["👥 团队成员：\n"]
        for m in members:
            tag = "（管理员）" if m.get("is_owner") else ""
            lines.append(f"· {m['name']}{tag}\n  {m['feishu_user_id']}")
        await send("\n".join(lines))
        return

    if cmd in ("/播报", "/broadcast"):
        period = parts[1] if len(parts) > 1 else "morning"
        from scheduler import _do_broadcast
        await _do_broadcast(period)
        await send(f"✅ 已触发{period}播报")
        return

    await send("可用指令：\n/添加成员 姓名 open_id\n/状态\n/成员\n/播报 morning|evening")


# ── Member private chat ───────────────────────────────────────────

async def _handle_member_private(
    open_id: str, sender_name: str,
    message_type: str, msg: dict, message_id: str, send: SendFn,
) -> None:
    await send("👀 收到，处理中…")
    text = await _extract_text(message_type, msg, send, message_id)
    if text is None:
        return
    text, _ = await _resolve_doc(text, send)
    if text in ("__already_processed__", "__error__"):
        return
    await _handle_content(text, sender_name, open_id, send, "private_chat")


# ── Group @mention ────────────────────────────────────────────────

async def _handle_group_mention(
    sender_open_id: str, sender_name: str,
    message_type: str, msg: dict, chat_id: str, message_id: str, send: SendFn,
) -> None:
    at_sender = f'<at user_id="{sender_open_id}"></at> '
    await send(f"{at_sender}👀 收到，记录中…")

    text = await _extract_text(message_type, msg, send, message_id)
    if not text:
        return

    text, doc_id = await _resolve_doc(text, send)
    if text in ("__already_processed__", "__error__"):
        return

    # Check parent message for doc URL if none found in current message
    if doc_id is None:
        parent_id = msg.get("parent_id") or msg.get("root_id")
        if parent_id:
            try:
                parent_raw = await fc.get_message_content(parent_id)
                if parent_raw:
                    try:
                        parent_text = json.loads(parent_raw).get("text", parent_raw)
                    except Exception:
                        parent_text = parent_raw
                    parent_text, parent_doc_id = await _resolve_doc(parent_text, send)
                    if parent_text not in ("__already_processed__", "__error__") and parent_doc_id:
                        text = parent_text
            except Exception as e:
                log.warning("Could not fetch parent message %s: %s", parent_id, e)

    await _handle_content(text, sender_name, sender_open_id, send, "group_chat", at_prefix=at_sender)


# ── Bot added to group ────────────────────────────────────────────

async def handle_bot_added_to_group(chat_id: str) -> None:
    welcome = (
        "大家好！我是彩虹屁踢屁股专家 👋\n\n"
        "我会帮你们管理项目任务、追踪进展、每天早晚定时催单——同时也会真心看见每一份努力。\n\n"
        "先来认识一下大家——请每位成员 @ 我做个自我介绍，说说你是谁、主要负责什么，例如：\n\n"
        "「@彩虹屁踢屁股专家 我是张三，主要做前端开发，负责 Web 端所有页面」\n\n"
        "认识你之后，我就能准确记录你的任务、在正确的时候 @ 你催单 💪"
    )
    await fc.send_to_chat(chat_id, welcome)
    log.info("Sent welcome message to group %s", chat_id)
