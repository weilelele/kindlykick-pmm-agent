import asyncio
import logging
import threading
from contextlib import asynccontextmanager

import lark_oapi as lark
import lark_oapi.ws.client as _lark_ws_client
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1
try:
    from lark_oapi.api.im.v1 import P2ImChatMemberBotAddedV1
    _HAS_BOT_ADDED_EVENT = True
except ImportError:
    _HAS_BOT_ADDED_EVENT = False
import json
from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import JSONResponse

import handlers
from config import settings
from scheduler import start_scheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


# ── WebSocket event → dict bridge ─────────────────────────────────

def _ws_event_to_body(data: P2ImMessageReceiveV1) -> dict:
    ev = data.event
    msg = ev.message if ev else None
    sender = ev.sender if ev else None

    mentions = []
    if msg and msg.mentions:
        for m in msg.mentions:
            mentions.append({
                "key": m.key,
                "id": {"open_id": m.id.open_id if m.id else ""},
                "name": m.name,
            })

    return {
        "schema": "2.0",
        "header": {"event_type": "im.message.receive_v1"},
        "event": {
            "sender": {
                "sender_id": {
                    "open_id": (sender.sender_id.open_id if sender and sender.sender_id else "")
                }
            },
            "message": {
                "chat_type": msg.chat_type if msg else "",
                "chat_id": msg.chat_id if msg else "",
                "message_id": msg.message_id if msg else "",
                "message_type": msg.message_type if msg else "",
                "content": msg.content if msg else "{}",
                "mentions": mentions,
            },
        },
    }


async def _safe_handle(body: dict) -> None:
    try:
        await handlers.handle_message(body)
    except Exception as e:
        log.error("Unhandled error in message handler: %s", e, exc_info=True)


async def _safe_bot_added(chat_id: str) -> None:
    try:
        await handlers.handle_bot_added_to_group(chat_id)
    except Exception as e:
        log.error("Unhandled error in bot_added handler: %s", e, exc_info=True)


# ── WebSocket client thread ────────────────────────────────────────

def _start_ws_thread(main_loop: asyncio.AbstractEventLoop) -> None:
    """Run in a daemon thread. Creates its own event loop for the SDK's blocking start()."""
    thread_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(thread_loop)
    # Redirect the SDK's module-level loop to this thread's loop
    _lark_ws_client.loop = thread_loop

    def on_message(data: P2ImMessageReceiveV1) -> None:
        msg = data.event.message if data.event else None
        msg_type = msg.message_type if msg else "?"
        log.info("WS RECV type=%s", msg_type)
        if msg_type in ("text", "post", "file", "image", "interactive"):
            body = _ws_event_to_body(data)
            asyncio.run_coroutine_threadsafe(_safe_handle(body), main_loop)

    builder = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(on_message)
    )

    if _HAS_BOT_ADDED_EVENT:
        def on_bot_added(data: "P2ImChatMemberBotAddedV1") -> None:
            chat_id = data.event.chat_id if data.event else ""
            if chat_id:
                log.info("WS RECV bot_added chat_id=%s", chat_id)
                asyncio.run_coroutine_threadsafe(_safe_bot_added(chat_id), main_loop)
        builder = builder.register_p2_im_chat_member_bot_added_v1(on_bot_added)

    handler = builder.build()
    cli = lark.ws.Client(
        settings.feishu_app_id,
        settings.feishu_app_secret,
        event_handler=handler,
        log_level=lark.LogLevel.DEBUG,
        auto_reconnect=True,
    )
    log.info("Feishu WebSocket connecting…")
    try:
        cli.start()  # blocks until disconnected
        log.warning("WebSocket client exited normally (unexpected)")
    except Exception as e:
        log.error("WebSocket client crashed: %s", e, exc_info=True)


# ── App lifespan ───────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    sched = start_scheduler()
    log.info("Scheduler started — broadcasts at 10:00 and 18:00 CST")

    main_loop = asyncio.get_event_loop()
    ws_thread = threading.Thread(
        target=_start_ws_thread, args=(main_loop,), daemon=True, name="feishu-ws"
    )
    ws_thread.start()

    yield
    sched.shutdown(wait=False)
    # ws_thread is daemon — it exits when the process exits


app = FastAPI(title="Feishu PM Agent", lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/webhook/event")
async def webhook_event(request: Request, background_tasks: BackgroundTasks):
    body_bytes = await request.body()
    body_str = body_bytes.decode("utf-8")

    try:
        body = json.loads(body_str)
    except json.JSONDecodeError:
        return JSONResponse({"code": 400})

    if body.get("type") == "url_verification":
        return JSONResponse({"challenge": body.get("challenge", "")})

    event_type = body.get("header", {}).get("event_type", "")
    log.info("WEBHOOK event_type=%s", event_type)

    if event_type == "im.message.receive_v1":
        msg_type = body.get("event", {}).get("message", {}).get("message_type", "")
        if msg_type in ("text", "post", "file", "image", "interactive"):
            background_tasks.add_task(_safe_handle, body)

    elif event_type == "im.chat.member.bot.added_v1":
        chat_id = body.get("event", {}).get("chat_id", "")
        if chat_id:
            background_tasks.add_task(_safe_bot_added, chat_id)

    return JSONResponse({"code": 0})
