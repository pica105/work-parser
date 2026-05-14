from __future__ import annotations

import re
from dataclasses import dataclass


MIN_SCORE = 2

KEYWORDS: dict[str, int] = {
    # Telegram / боты
    "telegram bot": 5,
    "telegram-бот": 5,
    "телеграм бот": 5,
    "телеграм-бот": 5,
    "тг бот": 5,
    "tg bot": 5,
    "бот в тг": 5,
    "бот для": 4,
    "чат-бот": 4,
    "bot api": 4,
    "telegram api": 4,
    "mini app": 4,
    "mini apps": 4,
    "web app": 3,
    "webapp": 3,
    # Парсинг / скрипты
    "парсер": 4,
    "парсинг": 4,
    "скрипт": 3,
    # Интеграции / автоматизация
    "автоматизация": 4,
    "автоматизировать": 3,
    "интеграция": 3,
    "интегрировать": 3,
    "интеграции": 3,
    # API / backend
    "api": 2,
    "rest api": 3,
    "restapi": 3,
    "backend": 3,
    "бекенд": 3,
    "back-end": 3,
    # DevOps / инфраструктура
    "devops": 4,
    "dev ops": 3,
    "ci/cd": 4,
    "ci cd": 4,
    "docker": 3,
    "docker compose": 3,
    "deploy": 3,
    "деплой": 3,
    "nginx": 3,
    "linux": 2,
    "сервер": 2,
    "настройка сервера": 3,
    "настройк": 2,
    "администрирование": 3,
    # Языки / фреймворки
    "python": 2,
    "django": 2,
    "fastapi": 2,
    "flask": 2,
    "aiohttp": 2,
    "asyncio": 2,
    "node.js": 2,
    "nodejs": 2,
    "typescript": 2,
    "javascript": 1,
    "go lang": 2,
    "golang": 2,
    "php": 1,
    "laravel": 2,
    # Базы данных / очереди
    "postgresql": 2,
    "postgres": 2,
    "redis": 2,
    "rabbitmq": 2,
    "kafka": 2,
    "sqlite": 1,
    "mysql": 1,
    # Микросервисы / доработки / багфиксы
    "микросервис": 4,
    "microservice": 4,
    "microservices": 4,
    "fix": 2,
    "bugfix": 3,
    "bug fix": 3,
    "багфикс": 3,
    "баг фикс": 3,
    "доработка": 3,
    "доработки": 3,
    "доработать": 3,
    "рефакторинг": 2,
    "refactoring": 2,
    "миграция": 2,
    "migration": 2,
    "оптимизация": 2,
    "оптимизировать": 2,
    "починить": 3,
    "исправить": 2,
    "ошибк": 2,
    # Битрикс24
    "битрикс24": 5,
    "bitrix24": 5,
    "bitrix 24": 4,
    # Нейросети / AI
    "gpt": 2,
    "openai": 2,
    "нейросеть": 2,
    "ai агент": 3,
    "ии агент": 3,
    # Общие
    "разработчик": 1,
    "проектная": 1,
    "разовая": 1,
    "удаленная работа": 1,
    "удаленка": 1,
    "проект": 1,
    "оплата": 1,
}

# Стоп-слова: full-time, офис, нетехнические специальности
STOP_WORDS: list[str] = [
    # Нетехнические
    "smm",
    "смм",
    "таргетолог",
    "директолог",
    "маркетолог",
    "копирайтер",
    "рерайтер",
    "дизайнер логотип",
    "иллюстратор",
    "монтажер",
    "рилсмейкер",
    "сторисмейкер",
    "ассистент",
    "менеджер по продажам",
    "оператор",
    "колл-центр",
    "набор текста",
    "расшифровка аудио",
    "отзывы",
    "оставлять отзывы",
    "лайки",
    "подписки",
    "без опыта",
    "ежедневные выплаты",
    # Full-time / офис / трудоустройство
    "full-time",
    "фултайм",
    "full time",
    "полный рабочий день",
    "трудоустройство",
    "оформление в штат",
    "employment",
    "офис",
    "только офис",
    "полный день в офисе",
    "в офисе",
    "работа в офисе",
    "штат",
    "в штат",
    # Сомнительные тематики
    "инвестиции",
    "ставки",
    "букмекер",
    "казино",
    "gambling",
    "onlyfans",
    "18+",
]


@dataclass(frozen=True)
class MatchResult:
    accepted: bool
    score: int
    matched_keywords: tuple[str, ...]
    rejected_by: tuple[str, ...]


def normalize(text: str) -> str:
    lowered = text.lower().replace("ё", "е")
    return re.sub(r"\s+", " ", lowered).strip()


def find_terms(text: str, terms: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    normalized = normalize(text)
    matches: list[str] = []
    for term in terms:
        term_normalized = normalize(term)
        if term_normalized and term_normalized in normalized:
            matches.append(term)
    return tuple(matches)


def match_text(text: str) -> MatchResult:
    rejected_by = find_terms(text, tuple(STOP_WORDS))
    if rejected_by:
        return MatchResult(False, 0, (), rejected_by)

    normalized = normalize(text)
    matched: list[str] = []
    score = 0
    for keyword, weight in KEYWORDS.items():
        keyword_norm = normalize(keyword)
        if keyword_norm and keyword_norm in normalized:
            matched.append(keyword)
            score += weight

    accepted = score >= MIN_SCORE
    return MatchResult(accepted, score, tuple(matched), ())
