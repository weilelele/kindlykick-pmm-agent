"""
Message event routing and processing logic.

All messages (text, file, image, post, Feishu doc URL) are routed through a
single Claude classify_message call that determines the intent:
  meeting_notes    → extract tasks via extract_tasks_from_meeting
  progress_update  → log progress and update task status
  bulk_done        → mark all sender's tasks as done
  task_assignment  → reassign specified tasks to a member
  query            → answer a question about project status

Auto-registration: unknown open_ids are looked up via Feishu API and added.
"""

from __future__ import annotations

import base64
import json
import re
import logging
from typing import Callable, Awaitable

import database as db
import ai_client as ai
import feishu_client as fc
from config import settings

log = logging.getLogger(__name__)

# Regex to detect Feishu doc URLs in text messages
_DOC_URL_RE = re.compile(
    r"https?://[a-zA-Z0-9\-]+\.(?:feishu\.cn|larkoffice\.com)/(?:docx|docs)/([A-Za-z0-9]+)"
)

SendFn = Callable[[str], Awaitable[None]]


# ── Top-level dispatcher ──────────────────────────────────────────

async def handle_message(event_body: dict) -> None:
    event = event_body.get("event", {})
    msg = event.get("message", {})
    sender = event.get("sender", {})

    chat_type = msg.get("chat_type")
    chat_id = msg.get("chat_id")
    message_id = msg.get("message_id", "")
    message_type = msg.get("message_type", "")
    sender_open_id = sender.get("sender_id", {}).get("open_id", "")

    if not sender_open_id or not chat_id:
        return

    member = await _ensure_member(sender_open_id)
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
            and m.get("key", "").startswith("@_user_")
            for m in mentions
        )
        if not bot_mentioned:
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
            is_owner = open_id == settings.owner_open_id
            member = db.create_member(name, open_id, is_owner)
            log.info("Auto-registered new member: %s (%s)", name, open_id)
            return member
    except Exception as e:
        log.warning("Could not auto-register %s: %s", open_id, e)
    return None


# ── Content extraction ────────────────────────────────────────────

async def _extract_text_content(
    message_type: str,
    msg: dict,
    send: SendFn,
    message_id: str,
) -> str | None:
    content_raw = msg.get("content", "{}")

    if message_type == "text":
        raw = json.loads(content_raw).get("text", "").strip()
        return re.sub(r"<at user_id=\"[^\"]+\">.*?</at>", "", raw).strip()

    elif message_type == "post":
        doc = json.loads(content_raw)
        parts: list[str] = []
        for lang_content in doc.values():
            title = lang_content.get("title", "")
            if title:
                parts.append(title)
            for block in lang_content.get("content", []):
                for elem in block:
                    if elem.get("tag") == "text":
                        parts.append(elem.get("text", ""))
        return "\n".join(parts).strip()

    elif message_type == "file":
        parsed = json.loads(content_raw)
        file_key = parsed.get("file_key", "")
        file_name = parsed.get("file_name", "")
        if not file_key:
            return None
        await send(f"⏳ 正在读取文件「{file_name}」…")
        file_bytes = await fc.download_file(message_id, file_key)
        if not file_bytes:
            await send("❌ 文件下载失败，请检查 bot 是否有 `im:resource` 权限。")
            return None
        try:
            return file_bytes.decode("utf-8")
        except UnicodeDecodeError:
            try:
                return file_bytes.decode("gbk")
            except Exception:
                await send("❌ 暂不支持此文件格式，请将内容复制为文本后发送，或发送飞书文档链接。")
                return None

    elif message_type == "image":
        parsed = json.loads(content_raw)
        image_key = parsed.get("image_key", "")
        if not image_key:
            return None
        await send("⏳ 正在识别图片内容…")
        image_bytes = await fc.download_image(message_id, image_key)
        if not image_bytes:
            await send("❌ 图片下载失败。")
            return None
        b64 = base64.standard_b64encode(image_bytes).decode()
        return await ai.extract_text_from_image(b64)

    return None


async def _fetch_doc_if_present(text: str, send: SendFn) -> tuple[str, str | None]:
    """
    If text contains a Feishu doc URL, fetch its content.
    Returns (final_text, doc_id_or_None).
    final_text has the doc URL replaced with the fetched content.
    If already processed, returns ("__already_processed__", doc_id).
    """
    match = _DOC_URL_RE.search(text)
    if not match:
        return text, None

    doc_id = match.group(1)
    if db.meeting_already_processed(doc_id):
        await send(f"ℹ️ 该文档（{doc_id[:8]}…）已处理过，任务列表保持不变。")
        return "__already_processed__", doc_id

    await send("⏳ 正在读取飞书文档…")
    content = await fc.get_feishu_doc_content(doc_id)
    if content is None:
        await send(
            "❌ 无法读取该飞书文档，请确认：\n"
            "1. 文档已分享给 bot 或设为「组织内可见」\n"
            "2. 应用已开启 `docx:document:readonly` 权限"
        )
        return "__error__", doc_id

    # Prepend doc_id tag so save_meeting can track it
    return f"[doc:{doc_id}]\n{content}", doc_id


# ── Unified intent handler ────────────────────────────────────────

async def _handle_content(
    text: str,
    sender_name: str,
    sender_open_id: str | None,
    send: SendFn,
    source: str,           # "group_chat" | "private_chat"
    at_prefix: str = "",   # e.g. '<at user_id="xxx"></at> ' for group replies
) -> None:
    """Route any extracted content through Claude intent classification."""
    tasks = db.get_active_tasks()
    members = db.get_members()

    action = await ai.classify_message(text, sender_name, tasks, members)
    action_type = action.get("action")
    log.info("classify_message: sender=%s action=%s", sender_name, action_type)

    if action_type == "meeting_notes":
        await _process_notes(text, send)

    elif action_type == "progress_update":
        task = _resolve_task(action.get("task_id_prefix"))
        member = db.get_member_by_open_id(sender_open_id) if sender_open_id else None
        # Fallback: if no task matched and sender has exactly 1 task
        if not task and sender_name:
            my_tasks = [t for t in tasks if t.get("assignee_name") == sender_name]
            if len(my_tasks) == 1:
                task = my_tasks[0]
                log.info("Fallback task match for %s: %s", sender_name, task["title"])
        db.add_progress_log(
            member_name=sender_name,
            content=action.get("progress_content", ""),
            source=source,
            task_id=task["id"] if task else None,
            member_id=member["id"] if member else None,
        )
        if action.get("new_status") and task:
            db.update_task(task["id"], {"status": action["new_status"]})
        await send(f"{at_prefix}{action.get('reply', '✅ 已记录')}")

    elif action_type == "bulk_done":
        target = action.get("target_name") or sender_name
        member = db.get_member_by_open_id(sender_open_id) if sender_open_id else None
        done_tasks = [
            t for t in tasks
            if t.get("assignee_name") == target and t.get("status") != "done"
        ]
        for t in done_tasks:
            db.update_task(t["id"], {"status": "done"})
        if done_tasks:
            db.add_progress_log(
                member_name=sender_name,
                content=f"批量完成 {len(done_tasks)} 项任务",
                source=source,
                task_id=None,
                member_id=member["id"] if member else None,
            )
            log.info("Bulk done: %s marked %d tasks done", target, len(done_tasks))
            lines = [f"{at_prefix}✅ 已将 {target} 的 {len(done_tasks)} 个任务标记完成："]
            lines += [f"  · {t['title']}" for t in done_tasks]
            await send("\n".join(lines))
        else:
            await send(f"{at_prefix}⚠️ {target} 当前没有未完成的任务。")

    elif action_type == "self_introduction":
        bio = action.get("bio", "")
        member = db.get_member_by_open_id(sender_open_id) if sender_open_id else None
        if not member:
            # _ensure_member was already called upstream, but just in case
            member = db.get_member_by_open_id(sender_open_id) if sender_open_id else None
        if member and bio:
            db.update_member_bio(sender_open_id, bio)
            log.info("Updated bio for %s: %s", sender_name, bio)

        reply = action.get("reply", f"✅ 认识你了，{sender_name}！")
        await send(f"{at_prefix}{reply}")

        # Review unassigned tasks: auto-assign any that mention this member's name
        matched = db.find_tasks_mentioning_name(sender_name)
        if matched and member:
            for t in matched:
                db.update_task(t["id"], {
                    "assignee_id": member["id"],
                    "assignee_name": member["name"],
                })
            lines = [f"顺便帮你认领了 {len(matched)} 个待分配任务（之前不认识你，暂时挂着呢）："]
            lines += [f"  · {t['title']}" for t in matched]
            await send(f"{at_prefix}" + "\n".join(lines))

    elif action_type == "task_assignment":
        assignee_name = action.get("assignee_name", "")
        target_member = next(
            (m for m in members if assignee_name in m["name"] or m["name"] in assignee_name),
            None,
        )
        if not target_member:
            await send(f"{at_prefix}❌ 未找到成员「{assignee_name}」，请先用 /添加成员 注册。")
            return
        updated = []
        for prefix in action.get("task_id_prefixes") or []:
            task = _resolve_task(prefix)
            if task:
                db.update_task(task["id"], {
                    "assignee_id": target_member["id"],
                    "assignee_name": target_member["name"],
                })
                updated.append(task)
        if updated:
            lines = [f"{at_prefix}✅ 已将 {len(updated)} 个任务分配给 {target_member['name']}："]
            lines += [f"  · {t['title']}" for t in updated]
            await send("\n".join(lines))
        else:
            await send(f"{at_prefix}⚠️ 未找到可匹配的任务，请确认任务标题。")

    else:
        # query or fallback
        question = action.get("question", text)
        answer = await ai.answer_query(question, tasks, members)
        await send(f"{at_prefix}{answer}")


# ── Task list formatting ──────────────────────────────────────────

def _format_task_list(tasks: list[dict]) -> str:
    if not tasks:
        return "📋 当前任务列表为空。"
    status_emoji = {"pending": "⏳", "in_progress": "🔄", "blocked": "🚫", "done": "✅"}
    prio_emoji = {"high": "🔴", "normal": "🟡", "low": "🟢", "urgent": "‼️"}
    by_assignee: dict[str, list] = {}
    for t in tasks:
        key = t.get("assignee_name") or "待分配"
        by_assignee.setdefault(key, []).append(t)
    lines = ["📋 当前任务列表：\n"]
    for name, ts in by_assignee.items():
        lines.append(f"【{name}】")
        for t in ts:
            se = status_emoji.get(t["status"], "📌")
            pe = prio_emoji.get(t.get("priority", "normal"), "🟡")
            due = f"  截止 {t['due_date']}" if t.get("due_date") else ""
            lines.append(f"  {se}{pe} {t['title']}{due}")
    return "\n".join(lines)


# ── Owner private chat ────────────────────────────────────────────

async def _handle_owner_message(
    message_type: str, msg: dict, open_id: str, message_id: str, send: SendFn
) -> None:
    content_raw = msg.get("content", "{}")

    # Slash commands (text only)
    if message_type == "text":
        raw_text = json.loads(content_raw).get("text", "").strip()
        if raw_text.startswith("/"):
            await _handle_command(raw_text, send)
            return

    await send("👀 收到，处理中…")

    text = await _extract_text_content(message_type, msg, send, message_id)
    if text is None:
        await send("暂不支持此消息类型。请发送：文字、飞书文档链接、文本文件或图片。")
        return

    # Fetch doc if URL present
    text, doc_id = await _fetch_doc_if_present(text, send)
    if text in ("__already_processed__", "__error__"):
        return

    await _handle_content(text, "管理员", open_id, send, "private_chat")


async def _handle_command(text: str, send: SendFn) -> None:
    parts = text.strip().split()
    cmd = parts[0].lower()

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
        tasks = db.get_active_tasks()
        await send(_format_task_list(tasks))
        return

    if cmd in ("/完成", "/done"):
        if len(parts) < 2:
            await send("用法：/完成 任务ID前缀（至少4位）")
            return
        task = _resolve_task(parts[1])
        if not task:
            await send(f"❌ 未找到任务：{parts[1]}")
            return
        db.update_task(task["id"], {"status": "done"})
        await send(f"✅ 已标记完成：{task['title']}")
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

    await send("可用指令：\n/添加成员 姓名 open_id\n/状态\n/完成 任务ID\n/成员\n/播报 morning|evening")


# ── Meeting notes / document processing ──────────────────────────

async def _process_notes(text: str, send: SendFn) -> None:
    """Extract tasks from meeting notes or any document, save, and return updated list."""
    # Strip [doc:xxx] prefix before passing to AI
    ai_text = re.sub(r"^\[doc:[A-Za-z0-9]+\]\n?", "", text, count=1)
    members = db.get_members()
    try:
        result = await ai.extract_tasks_from_meeting(ai_text, members)
    except Exception as e:
        await send(f"❌ AI 解析失败：{e}")
        return

    tasks_raw = result.get("tasks", [])
    summary = result.get("summary", "")
    title = result.get("meeting_title", "本次记录")

    existing_tasks = db.get_active_tasks()
    existing_titles = {t["title"].strip() for t in existing_tasks}

    to_insert = []
    for t in tasks_raw:
        task_title = (t.get("title") or "").strip()
        if not task_title or task_title in existing_titles:
            log.info("Skipping duplicate task: %s", task_title)
            continue
        assignee_id = None
        if t.get("assignee_open_id"):
            m = db.get_member_by_open_id(t["assignee_open_id"])
            if m:
                assignee_id = m["id"]
        to_insert.append({
            "title": task_title,
            "description": t.get("description"),
            "assignee_id": assignee_id,
            "assignee_name": t.get("assignee_name"),
            "priority": t.get("priority", "normal"),
            "due_date": t.get("due_date"),
            "status": "pending",
            "source": "meeting",
            "source_ref": title,
        })

    if to_insert:
        db.create_tasks_bulk(to_insert)
    db.save_meeting(title, text, summary, len(to_insert))

    prio_emoji = {"high": "🔴", "normal": "🟡", "low": "🟢"}
    lines = [f"✅ 「{title}」已处理\n\n📝 {summary}\n\n新增 {len(to_insert)} 个任务："]
    for t in tasks_raw[:15]:
        pe = prio_emoji.get(t.get("priority", "normal"), "🟡")
        assignee = t.get("assignee_name") or "待分配"
        due = f"（截止 {t['due_date']}）" if t.get("due_date") else ""
        lines.append(f"{pe} {t['title']}  →  {assignee}{due}")

    all_tasks = db.get_active_tasks()
    lines.append("\n" + _format_task_list(all_tasks))
    await send("\n".join(lines))


# ── Member private chat ───────────────────────────────────────────

async def _handle_member_private(
    open_id: str, sender_name: str,
    message_type: str, msg: dict, message_id: str, send: SendFn,
) -> None:
    await send("👀 收到，处理中…")
    text = await _extract_text_content(message_type, msg, send, message_id)
    if text is None:
        return

    text, doc_id = await _fetch_doc_if_present(text, send)
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

    text = await _extract_text_content(message_type, msg, send, message_id)
    if not text:
        return

    # Check current message for doc URL
    text, doc_id = await _fetch_doc_if_present(text, send)
    if text in ("__already_processed__", "__error__"):
        return

    # If no doc in current message, check parent message
    if doc_id is None:
        parent_id = msg.get("parent_id") or msg.get("root_id")
        if parent_id:
            try:
                parent_content_raw = await fc.get_message_content(parent_id)
                if parent_content_raw:
                    try:
                        parent_body = json.loads(parent_content_raw)
                        parent_text = parent_body.get("text", parent_content_raw)
                    except Exception:
                        parent_text = parent_content_raw
                    parent_text, parent_doc_id = await _fetch_doc_if_present(parent_text, send)
                    if parent_text in ("__already_processed__", "__error__"):
                        return
                    if parent_doc_id:
                        text = parent_text
            except Exception as e:
                log.warning("Could not fetch parent message %s: %s", parent_id, e)

    await _handle_content(text, sender_name, sender_open_id, send, "group_chat", at_prefix=at_sender)


# ── Bot added to group ───────────────────────────────────────────

async def handle_bot_added_to_group(chat_id: str) -> None:
    """Sent when the bot is first added to a group. Introduce itself and invite self-intros."""
    welcome = (
        "大家好！我是踢屁股专家 👋\n\n"
        "我会帮你们管理项目任务、追踪进展、每天早晚定时催单。\n\n"
        "先来认识一下大家——请每位成员 @ 我做个自我介绍，说说你是谁、主要负责什么，例如：\n\n"
        "「@踢屁股专家 我是张三，主要做前端开发，负责 Web 端所有页面」\n\n"
        "认识你之后，我就能准确记录你的任务、在正确的时候 @ 你催单 💪\n"
        "（如果会议记录里提到了你但还没自我介绍，待分配的任务会在你介绍完之后自动归到你名下）"
    )
    await fc.send_to_chat(chat_id, welcome)
    log.info("Sent welcome message to group %s", chat_id)


# ── Helpers ───────────────────────────────────────────────────────

def _resolve_task(prefix: str | None) -> dict | None:
    if not prefix:
        return None
    try:
        return db.find_task_by_id_prefix(prefix)
    except Exception:
        return None
