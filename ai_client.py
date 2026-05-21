from __future__ import annotations

import anthropic
from config import settings

_client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

_FAST_MODEL  = "claude-haiku-4-5"
_SMART_MODEL = "claude-sonnet-4-6"

_SYSTEM = [
    {
        "type": "text",
        "text": """你是"彩虹屁踢屁股专家"，一个服务于小型敏捷团队的 AI 项目管理助手。你有两个核心特质，缺一不可：

【踢屁股】催进度、盯截止日、对拖延零容忍，必要时直接点名施压。

【彩虹屁】真心看见每个人的努力和贡献，用具体、真诚的语言给予认可和鼓励。
  - 不是泛泛的"加油"，而是点出具体的事："你今天搞定了 XXX，这个挺难的，干得漂亮"
  - 每次成员汇报进展，都要在记录之外给一句真诚的看见

工作原则：
- 保持简洁，避免废话
- 在群组消息中使用飞书 at 格式 <at user_id="open_id"></at> 提及成员
- 只以中文回复
- 禁止使用 **加粗** 语法，飞书不渲染 Markdown
- 用 emoji、换行、全角符号做层次，不用 ** 包裹文字""",
        "cache_control": {"type": "ephemeral"},
    }
]

# ── Tool definitions ──────────────────────────────────────────────

_TOOLS = [
    {
        "name": "create_task",
        "description": "在多维表格中新建一个任务",
        "input_schema": {
            "type": "object",
            "properties": {
                "title":          {"type": "string", "description": "任务标题"},
                "assignee_name":  {"type": "string", "description": "负责人姓名（须是团队成员）"},
                "priority":       {"type": "string", "enum": ["🔴 高", "🟡 普通", "🟢 低"], "default": "🟡 普通"},
                "due_date":       {"type": "string", "description": "截止日期 YYYY-MM-DD，可选"},
                "notes":          {"type": "string", "description": "补充说明，可选"},
                "status":         {"type": "string", "enum": ["待开始", "进行中", "受阻", "已完成"], "default": "待开始"},
            },
            "required": ["title"],
        },
    },
    {
        "name": "update_tasks",
        "description": "更新一个或多个任务的字段（状态、负责人、优先级等）。filter 中至少提供一个条件",
        "input_schema": {
            "type": "object",
            "properties": {
                "filter": {
                    "type": "object",
                    "description": "筛选要更新哪些任务",
                    "properties": {
                        "titles":         {"type": "array", "items": {"type": "string"}, "description": "按任务标题列表模糊匹配"},
                        "assignee_name":  {"type": "string", "description": "按负责人筛选"},
                        "status":         {"type": "string", "description": "按状态筛选"},
                        "all_unassigned": {"type": "boolean", "description": "true = 所有未分配任务"},
                    },
                },
                "updates": {
                    "type": "object",
                    "description": "要写入的新值",
                    "properties": {
                        "assignee_name": {"type": "string"},
                        "status":        {"type": "string", "enum": ["待开始", "进行中", "受阻", "已完成"]},
                        "priority":      {"type": "string", "enum": ["🔴 高", "🟡 普通", "🟢 低"]},
                        "due_date":      {"type": "string"},
                        "notes":         {"type": "string"},
                    },
                },
            },
            "required": ["filter", "updates"],
        },
    },
    {
        "name": "delete_tasks",
        "description": "删除任务（去重或清理）。filter 中至少提供一个条件",
        "input_schema": {
            "type": "object",
            "properties": {
                "filter": {
                    "type": "object",
                    "properties": {
                        "titles":         {"type": "array", "items": {"type": "string"}, "description": "按标题模糊匹配"},
                        "assignee_name":  {"type": "string"},
                        "status":         {"type": "string"},
                        "all_unassigned": {"type": "boolean"},
                    },
                },
            },
            "required": ["filter"],
        },
    },
    {
        "name": "log_progress",
        "description": "记录成员进展，生成彩虹屁+催单回复",
        "input_schema": {
            "type": "object",
            "properties": {
                "member_name": {"type": "string"},
                "content":     {"type": "string", "description": "进展描述（20字以内）"},
                "task_title":  {"type": "string", "description": "关联任务标题，可选，模糊匹配"},
                "new_status":  {"type": "string", "enum": ["待开始", "进行中", "受阻", "已完成"], "description": "进展对应的新状态，可选"},
                "reply":       {"type": "string", "description": "先给彩虹屁（具体夸出做了什么），再记录或催下一步，2句以内"},
            },
            "required": ["member_name", "content", "reply"],
        },
    },
    {
        "name": "reply",
        "description": "向用户发送回复（当不需要任务操作时，或作为一系列操作后的总结）",
        "input_schema": {
            "type": "object",
            "properties": {
                "message": {"type": "string"},
            },
            "required": ["message"],
        },
    },
]


# ── Main entry point ──────────────────────────────────────────────

async def process_message(
    text: str,
    sender_name: str,
    tasks: list[dict],
    members: list[dict],
) -> list:
    """
    Route any message through Claude tool_use.
    Returns list of tool_use ContentBlock objects.
    Claude decides which tools to call (possibly multiple) and always ends with reply.
    """
    tasks_str = _fmt_tasks(tasks)
    members_str = "\n".join(
        f"- {m['name']} (open_id: {m['feishu_user_id']})" for m in members
    )

    resp = await _client.messages.create(
        model=_SMART_MODEL,
        max_tokens=4096,
        system=_SYSTEM,
        tools=_TOOLS,
        tool_choice={"type": "any"},
        messages=[
            {
                "role": "user",
                "content": f"""发送者：{sender_name}
消息内容：
{text}

当前任务（标题 | 负责人 | 状态 | 优先级 | 截止日）：
{tasks_str}

团队成员：
{members_str}

请根据消息内容调用合适的工具完成操作，可连续调用多个工具。最后必须调用 reply 工具发送回复。""",
            }
        ],
    )

    return [b for b in resp.content if b.type == "tool_use"]


def _fmt_tasks(tasks: list[dict]) -> str:
    if not tasks:
        return "（当前无活跃任务）"
    lines = []
    for t in tasks:
        due = f" | 截止 {t['due_date']}" if t.get("due_date") else ""
        lines.append(
            f"- {t['title']} | {t.get('assignee_name') or '未分配'} | {t['status']} | {t.get('priority', '🟡 普通')}{due}"
        )
    return "\n".join(lines)


# ── Daily broadcast ───────────────────────────────────────────────

async def generate_daily_summary(
    period: str,
    tasks: list[dict],
    today_progress: list[dict],
    members: list[dict],
) -> str:
    period_label = "早间（10:00）" if period == "morning" else "晚间（18:00）"
    period_instruction = (
        """这是早间播报。写法：
- 一句简短开场，有温度但不废话
- 按成员逐一 @ 并列出今日任务；如果昨天有进展记录，在他的任务前加一句具体的认可，自然带出
- 结尾一句话，给全队打气，具体有力"""
        if period == "morning"
        else
        """这是晚间播报。写法：
- 一句简短开场
- 按成员逐一 @：今天有进展的，先具体说出他做了什么，再列未完成任务；今天没进展的，直接点名催，语气可以不客气
- 不要设标题板块，一个个 @ 下去，自然流动
- 结尾简短收尾"""
    )

    status_label = {"待开始": "⏳待开始", "进行中": "🔄进行中", "受阻": "🚫受阻"}

    by_assignee: dict[str, list] = {}
    for t in tasks:
        key = t.get("assignee_name") or "待分配"
        by_assignee.setdefault(key, []).append(t)

    tasks_str = ""
    for name, ts in by_assignee.items():
        tasks_str += f"\n【{name}】\n"
        for t in ts:
            due = f"（截止 {t['due_date']}）" if t.get("due_date") else ""
            s = status_label.get(t["status"], t["status"])
            tasks_str += f"  · {t['title']} {s}{due}\n"

    progress_str = (
        "\n".join(f"- {p['member_name']}: {p['content']}" for p in today_progress)
        or "（今日暂无进展更新）"
    )

    member_info = "\n".join(
        f"- {m['name']}: open_id = {m['feishu_user_id']}" for m in members
    )

    resp = await _client.messages.create(
        model=_SMART_MODEL,
        max_tokens=1024,
        system=_SYSTEM,
        messages=[
            {
                "role": "user",
                "content": f"""生成{period_label}任务播报消息。

{period_instruction}

活跃任务：
{tasks_str}

今日进展记录：
{progress_str}

成员信息（@ 时必须严格使用下方提供的 open_id）：
{member_info}

@提及规则（非常重要）：
- @ 某人时，格式为 <at user_id="open_id"></at>，open_id 必须从上方成员列表中查找
- 绝对禁止使用 _user_1、_user_2 等格式

格式规则：
- 直接输出消息内容，不加任何前缀或说明
- 语言简洁有力，分段清晰，适当用 emoji
- 禁止使用 **加粗** 语法""",
            }
        ],
    )
    return resp.content[0].text


# ── Image OCR ─────────────────────────────────────────────────────

async def extract_text_from_image(image_b64: str) -> str:
    resp = await _client.messages.create(
        model=_FAST_MODEL,
        max_tokens=2048,
        system=_SYSTEM,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": "image/jpeg", "data": image_b64},
                    },
                    {
                        "type": "text",
                        "text": "请将图片中的所有文字内容完整提取出来，保持原有格式和层级结构，不要遗漏任何信息。只输出提取的文字，不要加任何说明。",
                    },
                ],
            }
        ],
    )
    return resp.content[0].text
