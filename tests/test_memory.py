import asyncio

from app.config import Settings
from app.db import Database
from app.memory import MemoryService
from app.waha import WahaClient


class FakeSummarizer:
    enabled = True

    async def summarize(self, existing_summary: str, transcript: str) -> str:
        return "Resumo consolidado da conversa."


def make_memory(tmp_path, **overrides):
    settings = Settings(
        database_path=str(tmp_path / "memory.db"),
        window_messages=overrides.pop("window_messages", 4),
        context_ttl_minutes=overrides.pop("context_ttl_minutes", 60),
        max_context_chars=overrides.pop("max_context_chars", 2400),
        max_stored_messages=overrides.pop("max_stored_messages", 200),
        summary_enabled=True,
        **overrides,
    )
    db = Database(settings.database_path)
    db.init()
    return db, MemoryService(db, settings), settings


def test_ttl_starts_a_new_context(tmp_path):
    db, memory, _ = make_memory(tmp_path, context_ttl_minutes=60)
    t0 = 1_000_000.0
    chat = "5517@c.us"

    memory.build_context(chat, "primeira mensagem", t0)
    memory.record_user(chat, "primeira mensagem", t0)
    memory.record_assistant(chat, "primeira resposta", t0)

    assert memory.db.message_count(chat) == 2

    ctx = memory.build_context(chat, "depois de muito tempo", t0 + 3601)
    assert ctx.history_count == 0
    assert ctx.summary == ""
    assert memory.db.message_count(chat) == 0

    db.close()


def test_window_overflow_is_summarized(tmp_path):
    db, memory, _ = make_memory(tmp_path, window_messages=4)
    t0 = 1_000_000.0
    chat = "5517@c.us"

    for index in range(6):
        memory.record_user(chat, f"pergunta {index}", t0 + index)
        memory.record_assistant(chat, f"resposta {index}", t0 + index + 0.1)

    assert memory.db.message_count(chat) == 12

    summary = asyncio.run(memory.maybe_summarize(chat, t0 + 10, FakeSummarizer()))

    assert summary == "Resumo consolidado da conversa."
    assert memory.db.message_count(chat) == 4
    conversation = memory.db.get_conversation(chat)
    assert conversation.summary == summary

    db.close()


def test_echo_guard_consumes_once(tmp_path):
    db, _, _ = make_memory(tmp_path)
    t0 = 1_000_000.0

    db.record_sent("resposta da Sophia", t0)
    assert db.consume_sent("resposta da Sophia", t0 - 5) is True
    assert db.consume_sent("resposta da Sophia", t0 - 5) is False

    db.close()


def test_context_budget_keeps_current_user_message(tmp_path):
    db, memory, _ = make_memory(tmp_path, max_context_chars=1500)
    t0 = 1_000_000.0
    chat = "5517@c.us"

    for index in range(10):
        memory.record_user(chat, "x" * 300, t0 + index)
        memory.record_assistant(chat, "y" * 300, t0 + index + 0.1)

    ctx = memory.build_context(chat, "pergunta atual relevante", t0 + 20)

    assert ctx.messages[-1]["role"] == "user"
    assert "pergunta atual relevante" in ctx.messages[-1]["content"]
    assert ctx.history_count < 20

    db.close()


def test_split_text_respects_limit():
    client = WahaClient(Settings(max_message_chars=200))
    chunks = client.split_text("a" * 450)

    assert len(chunks) >= 3
    assert all(len(chunk) <= 200 for chunk in chunks)
    assert "".join(chunks) == "a" * 450

    asyncio.run(client.close())


def test_self_chat_detection():
    from app.main import _is_self_chat

    base = Settings(allow_self_chat=True)
    assert _is_self_chat("5517@c.us", "5517@c.us", True, base) is True
    assert _is_self_chat("other@c.us", "5517@c.us", True, base) is False
    assert _is_self_chat("5517@c.us", "5517@c.us", False, base) is False

    explicit = Settings(allow_self_chat=True, self_chat_ids=["self@c.us"])
    assert _is_self_chat("self@c.us", "anything", True, explicit) is True
    assert _is_self_chat("other@c.us", "anything", True, explicit) is False
