"""SQLite persistence for per-chat short-term memory."""

from __future__ import annotations

import hashlib
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional


def hash_text(text: str) -> str:
    normalized = re.sub(r"\s+", " ", (text or "")).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Conversation:
    chat_id: str
    summary: str
    last_activity: float
    created_at: float


@dataclass(frozen=True)
class StoredMessage:
    id: int
    chat_id: str
    role: str
    content: str
    created_at: float


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.RLock()
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA foreign_keys=ON")

    # ------------------------------------------------------------------ schema
    def init(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS conversations (
                    chat_id TEXT PRIMARY KEY,
                    summary TEXT NOT NULL DEFAULT '',
                    last_activity REAL NOT NULL,
                    created_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id TEXT NOT NULL,
                    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
                    content TEXT NOT NULL,
                    wa_message_id TEXT,
                    created_at REAL NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_messages_chat_id
                    ON messages (chat_id, id);

                CREATE TABLE IF NOT EXISTS processed (
                    wa_message_id TEXT PRIMARY KEY,
                    created_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sent (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    text_hash TEXT NOT NULL,
                    created_at REAL NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_sent_hash
                    ON sent (text_hash, created_at);
                """
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------ conversations
    def get_conversation(self, chat_id: str) -> Optional[Conversation]:
        with self._lock:
            row = self._conn.execute(
                "SELECT chat_id, summary, last_activity, created_at "
                "FROM conversations WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()
        return _conversation_from_row(row) if row else None

    def get_or_create_conversation(
        self, chat_id: str, now: float, ttl_seconds: float
    ) -> Conversation:
        with self._lock:
            row = self._conn.execute(
                "SELECT chat_id, summary, last_activity, created_at "
                "FROM conversations WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO conversations (chat_id, summary, last_activity, created_at) "
                    "VALUES (?, '', ?, ?)",
                    (chat_id, now, now),
                )
                self._conn.commit()
                return Conversation(chat_id=chat_id, summary="", last_activity=now, created_at=now)

            conversation = _conversation_from_row(row)
            if ttl_seconds > 0 and (now - conversation.last_activity) > ttl_seconds:
                # Idle timeout: drop the old context and start a new conversation
                # with the same chat id.
                self._conn.execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,))
                self._conn.execute(
                    "UPDATE conversations SET summary = '', last_activity = ?, created_at = ? "
                    "WHERE chat_id = ?",
                    (now, now, chat_id),
                )
                self._conn.commit()
                return Conversation(chat_id=chat_id, summary="", last_activity=now, created_at=now)

            self._conn.execute(
                "UPDATE conversations SET last_activity = ? WHERE chat_id = ?",
                (now, chat_id),
            )
            self._conn.commit()
            return Conversation(
                chat_id=conversation.chat_id,
                summary=conversation.summary,
                last_activity=now,
                created_at=conversation.created_at,
            )

    def touch(self, chat_id: str, now: float) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE conversations SET last_activity = ? WHERE chat_id = ?",
                (now, chat_id),
            )
            self._conn.commit()

    def reset(self, chat_id: str, now: float) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,))
            self._conn.execute(
                "INSERT INTO conversations (chat_id, summary, last_activity, created_at) "
                "VALUES (?, '', ?, ?) "
                "ON CONFLICT(chat_id) DO UPDATE SET summary = '', last_activity = excluded.last_activity",
                (chat_id, now, now),
            )
            self._conn.commit()

    def set_summary(self, chat_id: str, summary: str, now: float) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO conversations (chat_id, summary, last_activity, created_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(chat_id) DO UPDATE SET summary = excluded.summary",
                (chat_id, summary, now, now),
            )
            self._conn.commit()

    # ----------------------------------------------------------------- messages
    def append_message(
        self,
        chat_id: str,
        role: str,
        content: str,
        now: float,
        wa_message_id: str | None = None,
    ) -> int:
        if role not in {"user", "assistant"}:
            raise ValueError(f"invalid role: {role}")
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO messages (chat_id, role, content, wa_message_id, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (chat_id, role, content, wa_message_id, now),
            )
            self._conn.commit()
            return int(cursor.lastrowid)

    def recent_messages(self, chat_id: str, limit: int) -> List[StoredMessage]:
        limit = max(0, int(limit))
        if limit == 0:
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, chat_id, role, content, created_at FROM messages "
                "WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
                (chat_id, limit),
            ).fetchall()
        return [_message_from_row(row) for row in reversed(rows)]

    def oldest_messages(self, chat_id: str, limit: int) -> List[StoredMessage]:
        limit = max(0, int(limit))
        if limit == 0:
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, chat_id, role, content, created_at FROM messages "
                "WHERE chat_id = ? ORDER BY id ASC LIMIT ?",
                (chat_id, limit),
            ).fetchall()
        return [_message_from_row(row) for row in rows]

    def message_count(self, chat_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM messages WHERE chat_id = ?", (chat_id,)
            ).fetchone()
        return int(row["n"]) if row else 0

    def delete_messages(self, message_ids: Iterable[int]) -> int:
        ids = [int(item) for item in message_ids]
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        with self._lock:
            cursor = self._conn.execute(
                f"DELETE FROM messages WHERE id IN ({placeholders})", ids
            )
            self._conn.commit()
            return cursor.rowcount

    # ------------------------------------------------------------------ dedupe
    def try_mark_processed(self, wa_message_id: str, now: float) -> bool:
        if not wa_message_id:
            return True
        with self._lock:
            cursor = self._conn.execute(
                "INSERT OR IGNORE INTO processed (wa_message_id, created_at) VALUES (?, ?)",
                (wa_message_id, now),
            )
            self._conn.commit()
            return cursor.rowcount == 1

    def unmark_processed(self, wa_message_id: str) -> None:
        if not wa_message_id:
            return
        with self._lock:
            self._conn.execute(
                "DELETE FROM processed WHERE wa_message_id = ?", (wa_message_id,)
            )
            self._conn.commit()

    def prune_processed(self, before: float) -> int:
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM processed WHERE created_at < ?", (before,)
            )
            self._conn.commit()
            return cursor.rowcount

    # -------------------------------------------------------------- echo guard
    def record_sent(self, text: str, now: float) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO sent (text_hash, created_at) VALUES (?, ?)",
                (hash_text(text), now),
            )
            self._conn.commit()

    def consume_sent(self, text: str, since: float) -> bool:
        """Return True and remove one matching outbound record."""
        text_hash = hash_text(text)
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM sent WHERE text_hash = ? AND created_at >= ? "
                "ORDER BY id ASC LIMIT 1",
                (text_hash, since),
            ).fetchone()
            if not row:
                return False
            self._conn.execute("DELETE FROM sent WHERE id = ?", (row["id"],))
            self._conn.commit()
            return True

    def prune_sent(self, before: float) -> int:
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM sent WHERE created_at < ?", (before,)
            )
            self._conn.commit()
            return cursor.rowcount

    # ------------------------------------------------------------------- stats
    def stats(self) -> dict:
        with self._lock:
            conversations = self._conn.execute(
                "SELECT COUNT(*) AS n FROM conversations"
            ).fetchone()["n"]
            messages = self._conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"]
            processed = self._conn.execute("SELECT COUNT(*) AS n FROM processed").fetchone()["n"]
            sent = self._conn.execute("SELECT COUNT(*) AS n FROM sent").fetchone()["n"]
        return {
            "conversations": int(conversations),
            "messages": int(messages),
            "processed": int(processed),
            "sent": int(sent),
        }


def _conversation_from_row(row: sqlite3.Row) -> Conversation:
    return Conversation(
        chat_id=row["chat_id"],
        summary=row["summary"],
        last_activity=float(row["last_activity"]),
        created_at=float(row["created_at"]),
    )


def _message_from_row(row: sqlite3.Row) -> StoredMessage:
    return StoredMessage(
        id=int(row["id"]),
        chat_id=row["chat_id"],
        role=row["role"],
        content=row["content"],
        created_at=float(row["created_at"]),
    )


def now_ts() -> float:
    return time.time()
