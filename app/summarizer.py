"""Rolling summary generation using an OpenAI-compatible endpoint."""

from __future__ import annotations

import logging
from typing import Optional

import httpx

from .config import Settings

logger = logging.getLogger("sophia.summarizer")

SUMMARY_SYSTEM_PROMPT = """Você resume conversas para memória de contexto de um agente.

Regras:
- Preserve fatos, nomes, números, datas, decisões, pendências, preferências e próximos passos.
- Não invente informação que não esteja na conversa.
- Não dê opinião, não faça recomendações e não responda ao usuário.
- Escreva em português do Brasil, em texto corrido, no máximo {max_words} palavras.
- Se houver um resumo anterior, incorpore-o sem repetir detalhes já consolidados."""


class Summarizer:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=10.0,
                read=settings.summary_timeout_seconds,
                write=30.0,
                pool=10.0,
            )
        )

    @property
    def enabled(self) -> bool:
        return bool(
            self.settings.summary_enabled
            and self.settings.summary_base_url
            and self.settings.summary_model
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def summarize(self, existing_summary: str, transcript: str) -> Optional[str]:
        if not self.enabled:
            return None

        prompt_parts = []
        if existing_summary:
            prompt_parts.append(f"Resumo anterior:\n{existing_summary}")
        prompt_parts.append(f"Conversa recente:\n{transcript}")
        prompt_parts.append("Gere o resumo consolidado.")

        payload = {
            "model": self.settings.summary_model,
            "messages": [
                {
                    "role": "system",
                    "content": SUMMARY_SYSTEM_PROMPT.format(
                        max_words=max(40, int(self.settings.summary_max_words))
                    ),
                },
                {"role": "user", "content": "\n\n".join(prompt_parts)},
            ],
            "temperature": 0.2,
            "max_tokens": max(160, int(self.settings.summary_max_words) * 3),
            "stream": False,
        }
        headers = {"Content-Type": "application/json"}
        if self.settings.summary_api_key:
            headers["Authorization"] = f"Bearer {self.settings.summary_api_key}"

        try:
            response = await self._client.post(
                self._chat_url(), json=payload, headers=headers
            )
        except Exception:
            logger.exception("summary request failed")
            return None

        if response.status_code >= 400:
            logger.warning(
                "summary endpoint status=%s body=%s",
                response.status_code,
                response.text[:300],
            )
            return None

        try:
            data = response.json()
            content = data["choices"][0]["message"]["content"]
        except Exception:
            logger.warning("summary response malformed")
            return None

        if isinstance(content, list):
            content = "".join(
                part.get("text", "")
                for part in content
                if isinstance(part, dict)
            )
        if not isinstance(content, str):
            return None
        content = content.strip()
        return content or None

    def _chat_url(self) -> str:
        base = self.settings.summary_base_url.rstrip("/")
        if not base.endswith("/v1"):
            base = f"{base}/v1"
        return f"{base}/chat/completions"
