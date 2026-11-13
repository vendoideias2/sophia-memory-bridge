"""Client for the Hermes API server."""

from __future__ import annotations

import logging
import re
from typing import Any, List

import httpx

from .config import Settings
from .memory import BuiltContext

logger = logging.getLogger("sophia.hermes")


class HermesError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class HermesClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=10.0,
                read=settings.hermes_timeout_seconds,
                write=30.0,
                pool=10.0,
            )
        )

    async def close(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------ public
    async def respond(
        self, chat_id: str, user_text: str, context: BuiltContext
    ) -> str:
        if self.settings.hermes_mode == "responses":
            try:
                return await self._responses(chat_id, user_text, context)
            except HermesError as exc:
                if exc.status_code in {400, 404, 405, 501}:
                    logger.warning(
                        "responses API unavailable (status=%s); falling back to chat",
                        exc.status_code,
                    )
                else:
                    raise
        return await self._chat(context)

    async def health(self) -> dict:
        url = self.settings.hermes_url("/v1/models")
        try:
            response = await self._client.get(url, headers=self._headers(), timeout=8.0)
            return {
                "ok": response.status_code < 400,
                "status": response.status_code,
                "url": url,
            }
        except Exception as exc:
            return {"ok": False, "status": None, "url": url, "error": str(exc)}

    # ------------------------------------------------------------------- modes
    async def _chat(self, context: BuiltContext) -> str:
        url = self.settings.hermes_url("/v1/chat/completions")
        payload = {
            "model": self.settings.hermes_model,
            "messages": context.messages,
            "stream": False,
        }
        data = await self._post(url, payload)
        try:
            content = data["choices"][0]["message"]["content"]
        except Exception as exc:
            raise HermesError(f"chat response malformed: {exc}") from exc
        return _coerce_content(content)

    async def _responses(
        self, chat_id: str, user_text: str, context: BuiltContext
    ) -> str:
        url = self.settings.hermes_url("/v1/responses")
        payload = {
            "model": self.settings.hermes_model,
            "input": user_text,
            "conversation": _conversation_name(chat_id),
            "store": True,
            "instructions": context.system_prompt,
        }
        data = await self._post(url, payload)

        output_text = data.get("output_text")
        if isinstance(output_text, str) and output_text.strip():
            return output_text.strip()

        parts: List[str] = []
        for item in data.get("output", []) or []:
            if item.get("type") != "message":
                continue
            for part in item.get("content", []) or []:
                if part.get("type") in {"output_text", "text"}:
                    text = part.get("text")
                    if isinstance(text, str):
                        parts.append(text)
        result = "".join(parts).strip()
        if not result:
            raise HermesError("responses API returned no text")
        return result

    # ------------------------------------------------------------------ helpers
    async def _post(self, url: str, payload: dict) -> dict:
        try:
            response = await self._client.post(url, json=payload, headers=self._headers())
        except Exception as exc:
            raise HermesError(f"request failed: {exc}") from exc

        if response.status_code >= 400:
            body = response.text[:600]
            raise HermesError(
                f"hermes status={response.status_code} body={body}",
                status_code=response.status_code,
            )
        try:
            return response.json()
        except Exception as exc:
            raise HermesError(f"invalid JSON from hermes: {exc}") from exc

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.settings.hermes_api_key:
            headers["Authorization"] = f"Bearer {self.settings.hermes_api_key}"
        return headers


def _coerce_content(content: Any) -> str:
    if isinstance(content, str):
        text = content.strip()
    elif isinstance(content, list):
        text = "".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict)
        ).strip()
    else:
        text = ""
    if not text:
        raise HermesError("hermes returned empty content")
    return text


def _conversation_name(chat_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", chat_id).strip("_")
    return f"wa_{safe}"[:64]
