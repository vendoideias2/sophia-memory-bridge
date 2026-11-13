"""Configuration for the Sophia memory bridge.

Everything is read from environment variables so the service can be deployed
without code changes. Secrets are never logged.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List

DEFAULT_SYSTEM_PROMPT = """Você é Sophia, assistente executiva do Fabiano e diretora de operações.

Regras de conversa:
- Use o histórico recente apenas como contexto. A última mensagem do usuário define o assunto atual.
- Se o usuário mudar de assunto, acompanhe imediatamente o novo assunto, sem insistir no anterior.
- Não peça informações que já estão no histórico; use-as.
- Se faltar um dado essencial, faça uma pergunta curta e específica.
- Seja direta, objetiva e resolutiva. Não repita saudações a cada mensagem.
- Quando o usuário pedir para agendar, confirmar, cancelar ou lembrar algo, registre explicitamente data, hora e próximo passo na resposta.
- Se uma ferramenta falhar, diga o que falhou e qual é o próximo passo; não invente confirmações."""


def _env(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return default if value is None or value == "" else value


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "y"}


def _env_list(name: str, default: List[str] | None = None) -> List[str]:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return list(default or [])
    return [item.strip() for item in raw.split(",") if item.strip()]


@dataclass(frozen=True)
class Settings:
    app_name: str = "sophia-memory-bridge"
    host: str = "0.0.0.0"
    port: int = 8080
    log_level: str = "INFO"
    log_message_bodies: bool = False

    database_path: str = "/data/memory.db"

    # Auth for this bridge. The webhook can also use WEBHOOK_SECRET.
    bridge_api_key: str = ""
    admin_api_key: str = ""
    webhook_secret: str = ""

    # Hermes API
    hermes_base_url: str = "http://hermes:8642"
    hermes_profile: str = "fabiano"
    hermes_api_key: str = ""
    hermes_model: str = "hermes-agent"
    hermes_mode: str = "chat"  # chat | responses
    hermes_timeout_seconds: float = 180.0

    # Short-term memory
    context_ttl_minutes: int = 360
    window_messages: int = 20
    max_context_chars: int = 24000
    max_stored_messages: int = 200

    # Rolling summary
    summary_enabled: bool = True
    summary_base_url: str = "https://free.vendoideias.com/v1"
    summary_api_key: str = ""
    summary_model: str = "auto"
    summary_max_words: int = 120
    summary_timeout_seconds: float = 30.0

    # WAHA
    waha_base_url: str = "https://waha.vendoideias.com"
    waha_api_key: str = ""
    waha_session: str = "FABIANO"
    waha_timeout_seconds: float = 30.0
    max_message_chars: int = 3500

    # Behaviour
    allow_groups: bool = False
    allow_self_chat: bool = True
    self_chat_ids: List[str] = field(default_factory=list)
    echo_window_seconds: int = 120
    process_media: bool = False
    dedupe_ttl_hours: int = 24

    system_prompt: str = DEFAULT_SYSTEM_PROMPT

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            app_name=_env("APP_NAME", cls.app_name),
            host=_env("HOST", cls.host),
            port=_env_int("PORT", cls.port),
            log_level=_env("LOG_LEVEL", cls.log_level).upper(),
            log_message_bodies=_env_bool("LOG_MESSAGE_BODIES", cls.log_message_bodies),
            database_path=_env("DATABASE_PATH", cls.database_path),
            bridge_api_key=_env("BRIDGE_API_KEY", cls.bridge_api_key),
            admin_api_key=_env("ADMIN_API_KEY", cls.admin_api_key),
            webhook_secret=_env("WEBHOOK_SECRET", cls.webhook_secret),
            hermes_base_url=_env("HERMES_BASE_URL", cls.hermes_base_url),
            hermes_profile=_env("HERMES_PROFILE", cls.hermes_profile),
            hermes_api_key=_env("HERMES_API_KEY", cls.hermes_api_key),
            hermes_model=_env("HERMES_MODEL", cls.hermes_model),
            hermes_mode=_env("HERMES_MODE", cls.hermes_mode).lower(),
            hermes_timeout_seconds=_env_float(
                "HERMES_TIMEOUT_SECONDS", cls.hermes_timeout_seconds
            ),
            context_ttl_minutes=_env_int("CONTEXT_TTL_MINUTES", cls.context_ttl_minutes),
            window_messages=_env_int("WINDOW_MESSAGES", cls.window_messages),
            max_context_chars=_env_int("MAX_CONTEXT_CHARS", cls.max_context_chars),
            max_stored_messages=_env_int(
                "MAX_STORED_MESSAGES", cls.max_stored_messages
            ),
            summary_enabled=_env_bool("SUMMARY_ENABLED", cls.summary_enabled),
            summary_base_url=_env("SUMMARY_BASE_URL", cls.summary_base_url),
            summary_api_key=_env(
                "SUMMARY_API_KEY", _env("OPENAI_API_KEY", cls.summary_api_key)
            ),
            summary_model=_env("SUMMARY_MODEL", cls.summary_model),
            summary_max_words=_env_int("SUMMARY_MAX_WORDS", cls.summary_max_words),
            summary_timeout_seconds=_env_float(
                "SUMMARY_TIMEOUT_SECONDS", cls.summary_timeout_seconds
            ),
            waha_base_url=_env("WAHA_BASE_URL", cls.waha_base_url).rstrip("/"),
            waha_api_key=_env("WAHA_API_KEY", cls.waha_api_key),
            waha_session=_env("WAHA_SESSION", cls.waha_session),
            waha_timeout_seconds=_env_float(
                "WAHA_TIMEOUT_SECONDS", cls.waha_timeout_seconds
            ),
            max_message_chars=_env_int("MAX_MESSAGE_CHARS", cls.max_message_chars),
            allow_groups=_env_bool("ALLOW_GROUPS", cls.allow_groups),
            allow_self_chat=_env_bool("ALLOW_SELF_CHAT", cls.allow_self_chat),
            self_chat_ids=_env_list("SELF_CHAT_IDS"),
            echo_window_seconds=_env_int("ECHO_WINDOW_SECONDS", cls.echo_window_seconds),
            process_media=_env_bool("PROCESS_MEDIA", cls.process_media),
            dedupe_ttl_hours=_env_int("DEDUPE_TTL_HOURS", cls.dedupe_ttl_hours),
            system_prompt=_env("SYSTEM_PROMPT", cls.system_prompt),
        )

    # ------------------------------------------------------------------ helpers
    @property
    def context_ttl_seconds(self) -> float:
        return max(0, self.context_ttl_minutes) * 60.0

    @property
    def dedupe_ttl_seconds(self) -> float:
        return max(1, self.dedupe_ttl_hours) * 3600.0

    def hermes_url(self, path: str) -> str:
        """Build a Hermes API URL, accounting for non-default profiles.

        Hermes exposes the default profile at /v1/* and named profiles at
        /p/<profile>/v1/*.
        """
        base = self.hermes_base_url.rstrip("/")
        profile = (self.hermes_profile or "").strip()
        if profile and profile.lower() not in {"default", "root"}:
            return f"{base}/p/{profile}{path}"
        return f"{base}{path}"

    def safe_summary(self) -> dict:
        """Non-secret configuration for /health and logs."""
        return {
            "hermes_base_url": self.hermes_base_url,
            "hermes_profile": self.hermes_profile or "default",
            "hermes_mode": self.hermes_mode,
            "hermes_model": self.hermes_model,
            "waha_base_url": self.waha_base_url,
            "waha_session": self.waha_session,
            "context_ttl_minutes": self.context_ttl_minutes,
            "window_messages": self.window_messages,
            "max_context_chars": self.max_context_chars,
            "summary_enabled": self.summary_enabled,
            "summary_model": self.summary_model,
            "allow_groups": self.allow_groups,
            "allow_self_chat": self.allow_self_chat,
        }
