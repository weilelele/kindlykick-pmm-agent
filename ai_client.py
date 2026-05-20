from __future__ import annotations

import json
import re
import anthropic
from config import settings

_client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

# Use Haiku for routing/classification/simple tasks (4-5x cheaper than Sonnet)
# Use Sonnet only for high-quality generation (broadcasts)
_FAST_MODEL = "claude-haiku-4-5"
_SMART_MODEL = "claude-sonnet-4-6"

# System prompt cached across all calls (saves tokens on repeated API calls)
_SYSTEM = [
    {
        "type": "text",
        "text": """你是"踢屁股专家"，一个服务于小型敏捷团队的 AI 项目管理助手。你有两个核心特质，缺一不可：

【踢屁股】催进度、盯截止日、对拖延零容忍，必要时直接点名施压。

【彩虹屁】真心看见每个人的努力和贡献，用具体、真诚的语言给予认可和鼓励。
  - 不是泛泛的"加油"，而是点出具体的事："你今天搞定了 XXX，这个挺难的，干得漂亮"
  - 每次成员汇报进展，都要在记录之外给一句真诚的看见
  - 播报里不只有任务清单，还有对每个人近期付出的认可
  - 让团队感受到：努力是被看见的，不只是一台完成任务的机器

核心职责：
1. 从会议记录中提取并结构化待办任务
2. 跟踪和更新成员的工作进展
3. 生成有温度的每日播报——既督促又激励
4. 在每一次互动中，既是严格的项目管理者，也是团队的啦啦队长

工作原则：
- 保持简洁，避免废话
- 提取任务时尽量明确负责人和截止时间
- 在群组消息中使用飞书 at 格式 <at user_id="open_id"></at> 提及成员
- 回复时先给彩虹屁（看见+鼓励），再踢屁股（催进度）
- 只以中文回复""",
        "cache_control": {"type": "ephemeral"},
    }
]


def _parse_json(text: str) -> dict | list:
    from json_repair import repair_json
    match = re.search(r"(\{.*\}|\[.*\])", text, re.DOTALL)
    if match:
        text = match.group()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        repaired = repair_json(text, return_objects=True)
        if isinstance(repaired, (dict, list)):
            return repaired
        raise


async def extract_tasks_from_meeting(meeting_notes: str, members: list[dict]) -> dict:
    """Parse meeting notes → structured task list + summary."""
    members_str = "\n".join(
        f"- {m['name']}（open_id: {m['feishu_user_id']}）" for m in members
    )

    resp = await _client.messages.create(
        model=_FAST_MODEL,
        max_tokens=2048,
        system=_SYSTEM,
        messages=[
            {
                "role": "user",
                "content": f"""请从以下会议记录中提取所有待办任务。

团队成员：
{members_str}

会议记录：
{meeting_notes}

以 JSON 格式返回（只返回 JSON，无其他文字）：
{{
  "meeting_title": "会议主题（简短）",
  "summary": "会议要点（2-3句话）",
  "tasks": [
    {{
      "title": "任务标题（简洁）",
      "description": "补充说明或 null",
      "assignee_name": "负责人姓名或 null",
      "assignee_open_id": "飞书 open_id 或 null",
      "priority": "high | normal | low",
      "due_date": "YYYY-MM-DD 或 null"
    }}
  ]
}}""",
            }
        ],
    )
    return _parse_json(resp.content[0].text)


async def classify_message(
    text: str,
    sender_name: str,
    tasks: list[dict],
    members: list[dict],
) -> dict:
    """
    Unified intent classifier. Pass any message content (plain text, fetched doc,
    extracted image text, etc.) and let the model decide what action to take.

    Returns one of:
      {"action": "meeting_notes"}
      {"action": "progress_update", "task_id_prefix", "progress_content", "new_status", "reply"}
      {"action": "bulk_done", "target_name", "reply"}
      {"action": "task_assignment", "task_id_prefixes": [...], "assignee_name", "reply"}
      {"action": "query", "question"}
    """
    tasks_str = "\n".join(
        f"- [{t['id'][:8]}] {t['title']} | {t['status']} | "
        f"负责人: {t.get('assignee_name') or '未分配'}"
        for t in tasks
    ) or "（当前无活跃任务）"

    members_str = "\n".join(
        f"- {m['name']} (open_id: {m['feishu_user_id']})" for m in members
    )

    resp = await _client.messages.create(
        model=_FAST_MODEL,
        max_tokens=1024,
        system=_SYSTEM,
        messages=[
            {
                "role": "user",
                "content": f"""分析以下消息，理解发送者的真实意图，返回对应操作指令。

发送者：{sender_name}
消息内容：
{text}

当前活跃任务（格式：[ID前8位] 标题 | 状态 | 负责人）：
{tasks_str}

团队成员：
{members_str}

请判断属于以下哪种操作，只返回对应 JSON，不加说明：

① 会议记录 / 任务提取 — 消息包含会议讨论、项目计划、待办列表等需要提取新任务的内容
{{"action": "meeting_notes"}}

② 进展更新 — 某人汇报某个任务的进度或完成情况
{{
  "action": "progress_update",
  "task_id_prefix": "从上方列表中匹配到的任务ID前8位，或 null",
  "progress_content": "进展描述（20字以内）",
  "new_status": "pending | in_progress | done | blocked | null",
  "reply": "先给彩虹屁：真心看见并夸出具体的事（1句），再记录进展或催下一步（1句）。合计不超过2句。"
}}

③ 批量完成 — 某人说自己的多项任务都完成了，想全部清掉
{{
  "action": "bulk_done",
  "target_name": "任务负责人姓名（通常是发送者，或消息中明确提到的人）",
  "reply": "简短确认"
}}

④ 任务分配 — 要求将某些任务分配给某个成员
{{
  "action": "task_assignment",
  "task_id_prefixes": ["从上方列表中匹配到的任务ID前8位列表"],
  "assignee_name": "目标成员姓名",
  "reply": "简短确认"
}}

⑤ 自我介绍 — 成员向 Bot 介绍自己是谁、做什么的
{{
  "action": "self_introduction",
  "bio": "提炼的角色/职责描述（20字以内，如：前端开发，负责 Web 端）",
  "reply": "热情欢迎，提到对方名字和角色"
}}

⑥ 问询 — 询问项目状态、任务进度或其他问题
{{"action": "query", "question": "问题原文"}}

判断要点：
- 优先理解语义意图，不依赖格式
- 进展更新中，"完成/弄完/搞定/做好了"→ new_status = done；"在做/进行中" → in_progress；"卡住/受阻" → blocked
- 任务匹配用语义相关性，不要求完全一致
- 如果消息又长又像会议讨论，选 meeting_notes
- 如果消息是"我是xxx，做xxx"/"大家好，我负责xxx"等自我介绍句式，选 self_introduction""",
            }
        ],
    )
    return _parse_json(resp.content[0].text)


async def generate_daily_summary(
    period: str,
    tasks: list[dict],
    today_progress: list[dict],
    members: list[dict],
) -> str:
    """Generate the 10:00 or 18:00 broadcast message for the group."""
    period_label = "早间（10:00）" if period == "morning" else "晚间（18:00）"
    period_instruction = (
        """这是早间播报。结构如下：
1. 开场：一句有温度的早安，让人感觉今天充满可能
2. 【昨日彩虹屁】：点名表扬昨天有进展更新的成员，具体说出他们做了什么、为什么值得被看见（没有进展记录则跳过）
3. 【今日任务】：按成员列出今日主要任务，语气积极，像在给队友加油，不是下达命令
4. 结尾：一句激励全队的话，有力量但不空洞"""
        if period == "morning"
        else
        """这是晚间播报。结构如下：
1. 开场：一句轻松的收工问候
2. 【今日彩虹屁】：真心点名表扬今天有进展的成员，具体说出他们完成了什么、付出了什么，让努力被看见
3. 【未完成任务催单】：对今日无进展或任务未完成的成员直接点名施压，语气可以不客气，但不要人身攻击
4. 结尾：简短收尾，明天继续"""
    )

    # Group tasks by assignee
    by_assignee: dict[str, list] = {}
    for t in tasks:
        key = t.get("assignee_name") or "待分配"
        by_assignee.setdefault(key, []).append(t)

    tasks_str = ""
    for name, ts in by_assignee.items():
        tasks_str += f"\n【{name}】\n"
        for t in ts:
            due = f"（截止 {t['due_date']}）" if t.get("due_date") else ""
            status_map = {"pending": "⏳待开始", "in_progress": "🔄进行中", "blocked": "🚫受阻"}
            s = status_map.get(t["status"], t["status"])
            tasks_str += f"  · {t['title']} {s}{due}\n"

    progress_str = (
        "\n".join(f"- {p['member_name']}: {p['content']}" for p in today_progress)
        or "（今日暂无进展更新）"
    )

    member_info = "\n".join(
        f"- {m['name']}: open_id = {m['feishu_user_id']}" for m in members
    )

    resp = await _client.messages.create(
        model=_SMART_MODEL,   # Sonnet: broadcast quality matters
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
- @ 某人时，格式为 <at user_id="open_id"></at>，其中 open_id 必须从上方成员列表中查找对应值
- 绝对禁止使用 _user_1、_user_2 等格式，那些是飞书文档内部的临时 key，不是真实 open_id
- 如果某成员不在上方列表中，只写名字，不要 @

格式规则（非常重要）：
- 直接输出消息内容，不加任何前缀或说明
- 对每个有未完成任务的成员用 @ 提醒
- 语言简洁有力，分段清晰
- 适当用 emoji 但不过多
- 晚间播报对今日无进展更新的成员可以适当点名
- 禁止使用 **加粗** 语法（即不要出现 ** 符号），飞书群消息不渲染 Markdown 加粗
- 用 emoji、全角符号、换行缩进来做层次和强调，不要用 ** 包裹文字""",
            }
        ],
    )
    return resp.content[0].text


async def extract_text_from_image(image_b64: str) -> str:
    """Use Claude vision to extract text/content from an image (e.g. screenshot of meeting notes)."""
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
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": image_b64,
                        },
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


async def answer_query(query: str, tasks: list[dict], members: list[dict]) -> str:
    """Answer a general project status question."""
    tasks_str = (
        "\n".join(
            f"- {t['title']} | {t['status']} | {t.get('assignee_name') or '未分配'}"
            for t in tasks
        )
        or "（无活跃任务）"
    )

    resp = await _client.messages.create(
        model=_FAST_MODEL,
        max_tokens=512,
        system=_SYSTEM,
        messages=[
            {
                "role": "user",
                "content": f"""回答以下问题，基于当前项目状态，简洁作答（不超过200字）。

问题：{query}

当前任务：
{tasks_str}""",
            }
        ],
    )
    return resp.content[0].text
