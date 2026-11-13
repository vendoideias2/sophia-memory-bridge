"""FastAPI service that adds recent conversational memory to Sophia on WhatsApp.

Flow:
    WAHA webhook -> this service -> Hermes API -> WAHA sendText

The service keeps a per-chat sliding window in SQLite, optionally rolls older
turns into a summary, and resets a conversation after an idle TTL or an explicit
/novo command.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .config import Settings
from .db import Database
from .hermes import HermesClient, HermesError
from .memory import BuiltContext, MemoryService
from .summarizer import Summarizer
from .waha import WahaClient, WahaError

logger = logging.getLogger("sophia")

RESET_COMMANDS = {"/novo", "novo", "nova conversa", "reiniciar", "reset", "/reset"}
MEDIA_REPLY = (
    "Recebi seu arquivo, mas neste canal eu processo texto. "
    "Escreva o que você precisa que eu resolvo."
)
ERROR_REPLY = (
    "Não consegui processar sua mensagem agora (erro temporário). "
    "Tente novamente em instantes."
)


# --------------------------------------------------------------------------- app
@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings.from_env()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    db = Database(settings.database_path)
    db.init()
    now = time.time()
    db.prune_processed(now - settings.dedupe_ttl_seconds)
    db.prune_sent(now - max(settings.echo_window_seconds * 4, 3600))

    app.state.settings = settings
    app.state.db = db
    app.state.memory = MemoryService(db, settings)
    app.state.hermes = HermesClient(settings)
    app.state.waha = WahaClient(settings)
    app.state.summarizer = Summarizer(settings)
    app.state.locks = {}

    logger.info(
        "startup %s profile=%s mode=%s db=%s",
        settings.app_name,
        settings.hermes_profile or "default",
        settings.hermes_mode,
        settings.database_path,
    )
    logger.info("config %s", settings.safe_summary())
    if not settings.hermes_api_key:
        logger.warning("HERMES_API_KEY is empty; Hermes calls will be rejected")
    if not settings.waha_api_key:
        logger.warning("WAHA_API_KEY is empty; WhatsApp sends will be rejected")

    try:
        yield
    finally:
        await app.state.hermes.close()
        await app.state.waha.close()
        await app.state.summarizer.close()
        db.close()
        logger.info("shutdown")


app = FastAPI(title="Sophia Memory Bridge", version="1.0.0", lifespan=lifespan)


# ------------------------------------------------------------------------ models
class ChatRequest(BaseModel):
    chat_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    reset: bool = False
    session: Optional[str] = None


class ChatResponse(BaseModel):
    reply: str
    chat_id: str
    summary: str
    history_count: int


class ResetRequest(BaseModel):
    confirm: bool = False


# ------------------------------------------------------------------------- auth
def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _extract_key(request: Request) -> str:
    key = request.headers.get("x-api-key") or ""
    if not key:
        auth = request.headers.get("authorization") or ""
        if auth.lower().startswith("bearer "):
            key = auth.split(" ", 1)[1].strip()
    if not key:
        key = request.query_params.get("api_key", "")
    return key


async def require_bridge(request: Request) -> None:
    settings = _settings(request)
    if settings.bridge_api_key and _extract_key(request) != settings.bridge_api_key:
        raise HTTPException(status_code=401, detail="invalid bridge api key")


async def require_admin(request: Request) -> None:
    settings = _settings(request)
    expected = settings.admin_api_key or settings.bridge_api_key
    if expected and _extract_key(request) != expected:
        raise HTTPException(status_code=401, detail="invalid admin api key")


async def require_webhook(request: Request, path_secret: Optional[str] = None) -> None:
    settings = _settings(request)
    if not settings.webhook_secret:
        return
    candidates = {
        path_secret or "",
        request.headers.get("x-webhook-secret", ""),
        request.query_params.get("secret", ""),
        _extract_key(request),
    }
    if settings.webhook_secret not in candidates:
        raise HTTPException(status_code=401, detail="invalid webhook secret")


# ----------------------------------------------------------------------- helpers
def _get_lock(request: Request, chat_id: str) -> asyncio.Lock:
    locks: Dict[str, asyncio.Lock] = request.app.state.locks
    if chat_id not in locks:
        locks[chat_id] = asyncio.Lock()
    return locks[chat_id]


def _payload_value(payload: dict, *keys: str, default: Any = None) -> Any:
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return value
    return default


def _inbound(body: dict) -> Optional[dict]:
    event = str(body.get("event") or body.get("type") or "").lower()
    if event not in {"message", "message.any"}:
        return None

    payload = body.get("payload") or body.get("data") or body
    if not isinstance(payload, dict):
        return None

    session = str(
        body.get("session") or payload.get("session") or ""
    ).strip()
    chat_id = str(
        _payload_value(payload, "chatId", "chat_id", "from", default="")
    ).strip()
    sender = str(_payload_value(payload, "from", "author", default="")).strip()
    wa_id = str(_payload_value(payload, "id", "messageId", default="")).strip()
    text = str(
        _payload_value(payload, "body", "text", "caption", default="")
    )
    from_me = bool(payload.get("fromMe"))
    is_group = bool(payload.get("isGroup")) or chat_id.endswith("@g.us")
    has_media = bool(payload.get("hasMedia") or payload.get("media"))
    media_type = str(payload.get("type") or ("media" if has_media else ""))

    return {
        "event": event,
        "session": session,
        "chat_id": chat_id,
        "sender": sender,
        "wa_id": wa_id,
        "text": text.strip(),
        "from_me": from_me,
        "is_group": is_group,
        "has_media": has_media,
        "media_type": media_type,
    }


def _is_self_chat(chat_id: str, sender: str, from_me: bool, settings: Settings) -> bool:
    if not from_me or not settings.allow_self_chat:
        return False
    if settings.self_chat_ids:
        return chat_id in settings.self_chat_ids
    return bool(sender) and chat_id == sender


async def _deliver(
    request: Request, session: str, chat_id: str, text: str
) -> None:
    settings: Settings = request.app.state.settings
    waha: WahaClient = request.app.state.waha
    db: Database = request.app.state.db

    chunks = waha.split_text(text)
    for chunk in chunks:
        # Record before sending so the WAHA echo cannot be mistaken for a user
        # message if it arrives before the send call returns.
        db.record_sent(chunk, time.time())
        await waha.send_chunk(session or settings.waha_session, chat_id, chunk)


async def _run_turn(
    request: Request, chat_id: str, text: str, session: str
) -> str:
    settings: Settings = request.app.state.settings
    memory: MemoryService = request.app.state.memory
    hermes: HermesClient = request.app.state.hermes
    summarizer: Summarizer = request.app.state.summarizer
    db: Database = request.app.state.db
    now = time.time()

    context: BuiltContext = memory.build_context(chat_id, text, now)
    memory.record_user(chat_id, text, now)
    if settings.log_message_bodies:
        logger.info("turn chat=%s in=%r", chat_id, text[:300])
    else:
        logger.info("turn chat=%s in_chars=%d history=%d", chat_id, len(text), context.history_count)

    reply = await hermes.respond(chat_id, text, context)

    memory.record_assistant(chat_id, reply, time.time())
    await memory.maybe_summarize(chat_id, time.time(), summarizer)
    db.touch(chat_id, time.time())
    return reply


# ---------------------------------------------------------------------- endpoints
@app.get("/")
async def root() -> dict:
    return {"service": "sophia-memory-bridge", "status": "ok"}


@app.get("/health")
async def health(request: Request, deep: bool = False) -> dict:
    settings: Settings = request.app.state.settings
    db: Database = request.app.state.db
    payload = {
        "status": "ok",
        "service": settings.app_name,
        "database": db.stats(),
        "config": settings.safe_summary(),
        "hermes_key_configured": bool(settings.hermes_api_key),
        "waha_key_configured": bool(settings.waha_api_key),
        "webhook_secret_configured": bool(settings.webhook_secret),
    }
    if deep:
        hermes = request.app.state.hermes
        waha = request.app.state.waha
        payload["checks"] = {
            "hermes": await hermes.health(),
            "waha": await waha.health(),
        }
    return payload


@app.post("/webhook/waha", response_model=None)
@app.post("/webhook/waha/{path_secret}", response_model=None)
async def waha_webhook(
    request: Request,
    path_secret: Optional[str] = None,
):
    await require_webhook(request, path_secret)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="invalid payload")

    inbound = _inbound(body)
    if inbound is None:
        return {"ok": True, "skipped": "event"}

    settings: Settings = request.app.state.settings
    db: Database = request.app.state.db
    memory: MemoryService = request.app.state.memory

    chat_id = inbound["chat_id"]
    if not chat_id:
        return {"ok": True, "skipped": "no_chat_id"}
    if chat_id == "status@broadcast" or chat_id.endswith("@newsletter"):
        return {"ok": True, "skipped": "status_or_newsletter"}
    if inbound["is_group"] and not settings.allow_groups:
        return {"ok": True, "skipped": "group"}

    if inbound["from_me"]:
        if not _is_self_chat(chat_id, inbound["sender"], True, settings):
            return {"ok": True, "skipped": "from_me"}
        if inbound["text"] and db.consume_sent(
            inbound["text"], time.time() - settings.echo_window_seconds
        ):
            return {"ok": True, "skipped": "echo"}

    text = inbound["text"]
    if not text:
        if inbound["has_media"]:
            if not settings.process_media:
                await _deliver(request, inbound["session"], chat_id, MEDIA_REPLY)
                return {"ok": True, "replied": "media"}
            text = f"[O usuário enviou uma mídia do tipo {inbound['media_type']}.]"
        else:
            return {"ok": True, "skipped": "empty"}

    wa_id = inbound["wa_id"]
    if wa_id and not db.try_mark_processed(wa_id, time.time()):
        return {"ok": True, "skipped": "duplicate"}

    lock = _get_lock(request, chat_id)
    async with lock:
        if text.strip().lower() in RESET_COMMANDS:
            memory.reset(chat_id, time.time())
            await _deliver(
                request,
                inbound["session"],
                chat_id,
                "Contexto reiniciado. Pode começar uma nova conversa.",
            )
            return {"ok": True, "reset": True}

        try:
            reply = await _run_turn(
                request, chat_id, text, inbound["session"]
            )
            await _deliver(request, inbound["session"], chat_id, reply)
            return {"ok": True}
        except HermesError as exc:
            logger.error("hermes error chat=%s status=%s: %s", chat_id, exc.status_code, exc)
            try:
                await _deliver(request, inbound["session"], chat_id, ERROR_REPLY)
            except Exception:
                logger.exception("failed to deliver error reply chat=%s", chat_id)
            return {"ok": False, "error": "hermes"}
        except WahaError as exc:
            logger.error("waha send error chat=%s: %s", chat_id, exc)
            return JSONResponse(
                status_code=502, content={"ok": False, "error": "waha_send"}
            )
        except Exception:
            logger.exception("unexpected error chat=%s", chat_id)
            if wa_id:
                db.unmark_processed(wa_id)
            raise HTTPException(status_code=500, detail="internal error")


@app.post("/v1/chat", response_model=ChatResponse)
async def chat(
    request: Request,
    payload: ChatRequest,
    _: None = Depends(require_bridge),
) -> ChatResponse:
    memory: MemoryService = request.app.state.memory
    lock = _get_lock(request, payload.chat_id)
    async with lock:
        if payload.reset:
            memory.reset(payload.chat_id, time.time())
        try:
            reply = await _run_turn(
                request, payload.chat_id, payload.text, payload.session or ""
            )
        except HermesError as exc:
            logger.error(
                "hermes error chat=%s status=%s: %s",
                payload.chat_id,
                exc.status_code,
                exc,
            )
            raise HTTPException(status_code=502, detail="hermes request failed")
        snapshot = memory.snapshot(payload.chat_id, limit=1)
        return ChatResponse(
            reply=reply,
            chat_id=payload.chat_id,
            summary=snapshot["summary"],
            history_count=memory.db.message_count(payload.chat_id),
        )


@app.post("/admin/reset/{chat_id:path}")
async def admin_reset(
    request: Request,
    chat_id: str,
    _: None = Depends(require_admin),
) -> dict:
    request.app.state.memory.reset(chat_id, time.time())
    return {"ok": True, "chat_id": chat_id}


@app.get("/admin/conversations/{chat_id:path}")
async def admin_conversation(
    request: Request,
    chat_id: str,
    limit: int = 50,
    _: None = Depends(require_admin),
) -> dict:
    return request.app.state.memory.snapshot(chat_id, limit=max(1, min(limit, 200)))
