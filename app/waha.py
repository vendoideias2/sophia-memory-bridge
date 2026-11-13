"""WAHA client used to send replies back to WhatsApp."""

from __future__ import annotations

import logging
from typing import List
from urllib.parse import quote

import httpx

from .config import Settings

logger = logging.getLogger("sophia.waha")


class WahaError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class WahaClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=10.0,
                read=settings.waha_timeout_seconds,
                write=30.0,
                pool=10.0,
            )
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def health(self) -> dict:
        url = f"{self.settings.waha_base_url}/api/sessions"
        try:
            response = await self._client.get(url, headers=self._headers(), timeout=8.0)
            return {"ok": response.status_code < 400, "status": response.status_code}
        except Exception as exc:
            return {"ok": False, "status": None, "error": str(exc)}

    async def send_text(self, session: str, chat_id: str, text: str) -> List[dict]:
        chunks = self.split_text(text)
        results: List[dict] = []
        for chunk in chunks:
            results.append(await self.send_chunk(session, chat_id, chunk))
        return results

    async def send_chunk(self, session: str, chat_id: str, text: str) -> dict:
        session_path = quote(session or self.settings.waha_session, safe="")
        url = f"{self.settings.waha_base_url}/api/{session_path}/sendText"
        payload = {"chatId": chat_id, "text": text}
        try:
            response = await self._client.post(
                url, json=payload, headers=self._headers()
            )
        except Exception as exc:
            raise WahaError(f"send failed: {exc}") from exc
        if response.status_code >= 400:
            raise WahaError(
                f"waha status={response.status_code} body={response.text[:400]}",
                status_code=response.status_code,
            )
        try:
            return response.json()
        except Exception:
            return {"status": response.status_code}

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.settings.waha_api_key:
            headers["X-Api-Key"] = self.settings.waha_api_key
        return headers

    def split_text(self, text: str) -> List[str]:
        limit = max(200, int(self.settings.max_message_chars))
        text = (text or "").replace("\r\n", "\n").strip()
        if not text:
            return []
        if len(text) <= limit:
            return [text]

        chunks: List[str] = []
        current: List[str] = []
        current_len = 0

        for line in text.split("\n"):
            pieces = [line] if len(line) <= limit else [
                line[i : i + limit] for i in range(0, len(line), limit)
            ]
            for piece in pieces:
                addition = len(piece) + (1 if current else 0)
                if current and current_len + addition > limit:
                    chunks.append("\n".join(current).strip())
                    current = []
                    current_len = 0
                current.append(piece)
                current_len += len(piece) + (1 if len(current) > 1 else 0)

        if current:
            chunks.append("\n".join(current).strip())
        return [chunk for chunk in chunks if chunk]
