import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

import database as db
import bitable_client as bc
import ai_client as ai
import feishu_client as fc

_tz = pytz.timezone("Asia/Shanghai")
scheduler = AsyncIOScheduler(timezone=_tz)


async def _do_broadcast(period: str) -> None:
    """Core logic: generate and send the daily broadcast. Called by cron and /播报 command."""
    tasks = await bc.list_active_records()
    today_progress = db.get_today_progress()
    members = db.get_members()

    if not tasks:
        msg = (
            "☀️ 早安！当前无待处理任务，继续保持！"
            if period == "morning"
            else "🌆 今日收工！无积压任务，干得漂亮！"
        )
        await fc.send_to_group(msg)
        return

    summary = await ai.generate_daily_summary(period, tasks, today_progress, members)
    await fc.send_to_group(summary)


def start_scheduler() -> AsyncIOScheduler:
    scheduler.add_job(
        _do_broadcast,
        CronTrigger(hour=10, minute=0, timezone=_tz),
        args=["morning"],
        id="morning_broadcast",
        replace_existing=True,
    )
    scheduler.add_job(
        _do_broadcast,
        CronTrigger(hour=18, minute=0, timezone=_tz),
        args=["evening"],
        id="evening_broadcast",
        replace_existing=True,
    )
    scheduler.start()
    return scheduler
