from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path


# ─── Загрузка конфигурации фильтров из JSON-файлов ───

_FILTERS_DIR = Path(__file__).parent / "filters_config"


def _load_keywords() -> dict[str, int]:
    """Загружает ключевые слова из keywords.json."""
    path = _FILTERS_DIR / "keywords.json"
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            print(f"❌ Ошибка в файле keywords.json: {e}")
            print(f"  Открой этот файл в редакторе и проверь:")
            print(f"  • Нет ли лишних запятых после последнего элемента")
            print(f"  • Все ли кавычки закрыты")
            print(f"  • Нет ли лишних символов")
            raise
        # Разворачиваем вложенную структуру {category: {word: score}} → {word: score}
        result: dict[str, int] = {}
        for group in data.values():
            result.update(group)
        return result
    return {}


def _load_stop_words() -> list[str]:
    """Загружает стоп-слова из stop_words.json."""
    path = _FILTERS_DIR / "stop_words.json"
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError as e:
            print(f"❌ Ошибка в файле stop_words.json: {e}")
            print(f"  Открой этот файл в редакторе и проверь:")
            print(f"  • Нет ли лишних запятых после последнего элемента")
            print(f"  • Все ли кавычки закрыты")
            print(f"  • Нет ли лишних символов")
            raise
    return []


MIN_SCORE = 2
KEYWORDS: dict[str, int] = _load_keywords()
STOP_WORDS: list[str] = _load_stop_words()


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
