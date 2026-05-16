from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _parse_kwork_criteria(raw: str) -> list[str]:
    """Парсит KWORK_CRITERIA из .env (разделитель — запятая)."""
    if not raw or not raw.strip():
        return []
    return [item.strip().lower() for item in raw.split(",") if item.strip()]


def _first_env(*names: str) -> str:
    for name in names:
        value = os.getenv(name)
        if value is not None and value.strip():
            return value.strip()
    return ""


@dataclass(frozen=True)
class RuntimeConfig:
    api_id: int
    api_hash: str
    bot_token: str
    database_path: Path
    user_session_path: Path
    bot_session_path: Path
    catch_up_limit: int
    send_catch_up: bool
    log_level: str
    admin_telegram_id: int
    csrf_user_token: str
    kwork_auth_cookies: str
    openrouter_api_key: str
    openrouter_model: str
    kwork_criteria: list[str]

    @classmethod
    def from_env(cls) -> "RuntimeConfig":
        load_dotenv()

        api_id_raw = _first_env("TELEGRAM_API_ID", "API_ID")
        api_hash = _first_env("TELEGRAM_API_HASH", "API_HASH")
        bot_token = _first_env("TELEGRAM_BOT_TOKEN", "BOT_TOKEN")
        missing = [
            name
            for name, value in {
                "TELEGRAM_API_ID/API_ID": api_id_raw,
                "TELEGRAM_API_HASH/API_HASH": api_hash,
                "TELEGRAM_BOT_TOKEN/BOT_TOKEN": bot_token,
            }.items()
            if not value
        ]
        if missing:
            joined = ", ".join(missing)
            raise RuntimeError(
                f"❌ Не заполнены обязательные переменные в .env: {joined}\n"
                f"Открой файл .env и впиши их.\n"
                f"TELEGRAM_API_ID и TELEGRAM_API_HASH — из my.telegram.org\n"
                f"TELEGRAM_BOT_TOKEN — из @BotFather"
            )

        try:
            api_id = int(api_id_raw)
        except ValueError as exc:
            raise RuntimeError(
                f"❌ TELEGRAM_API_ID должно быть числом, а не '{api_id_raw}'\n"
                f"Открой .env и исправь значение TELEGRAM_API_ID"
            ) from exc

        return cls(
            api_id=api_id,
            api_hash=api_hash,
            bot_token=bot_token,
            database_path=Path(os.getenv("DATABASE_PATH", "data/leads.sqlite3")),
            user_session_path=Path(os.getenv("USER_SESSION_PATH", "sessions/freelancer_user")),
            bot_session_path=Path(os.getenv("BOT_SESSION_PATH", "sessions/freelancer_delivery_bot")),
            catch_up_limit=max(0, int(os.getenv("CATCH_UP_LIMIT", "25"))),
        send_catch_up=_bool_env("SEND_CATCH_UP", True),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        admin_telegram_id=int(os.getenv("ADMIN_TELEGRAM_ID", "0")),
        csrf_user_token=os.getenv("CSRF_USER_TOKEN", ""),
        kwork_auth_cookies=os.getenv("KWORK_AUTH_COOKIES", ""),
        openrouter_api_key=os.getenv("OPENROUTER_API_KEY", ""),
        openrouter_model=os.getenv("OPENROUTER_MODEL", "openai/gpt-4o-mini"),
        kwork_criteria=_parse_kwork_criteria(os.getenv("KWORK_CRITERIA", "")),
        )
