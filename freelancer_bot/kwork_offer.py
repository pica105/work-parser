from __future__ import annotations

import json
import logging
import random
import re
import string
from pathlib import Path
from typing import Optional
from openrouter import OpenRouter

import httpx
from telethon.tl.custom.message import Message

from .sources import Source
from .storage import LeadRecord

LOGGER = logging.getLogger("freelancer_bot.kwork")

PROFILE_PATH = Path("profile.md")

# Kwork project ID from URL like https://kwork.ru/projects/3175656?ref=...
KWORK_PROJECT_RE = re.compile(r"kwork\.ru/projects/(\d+)")

# Budget patterns — ищем в блоке с ценой проекта
BUDGET_RE = re.compile(
    r"(?:от\s*)?(\d[\d\s]*)\s*(?:руб|₽|\$|eur|usd)",
    re.IGNORECASE,
)
BUDGET_RANGE_RE = re.compile(
    r"(\d[\d\s]*)\s*(?:[-–—]|до)\s*(\d[\d\s]*)",
    re.IGNORECASE,
)

# Title from project page
TITLE_RE = re.compile(r"<title>([^<]+)</title>")
# Description containers
DESCRIPTION_RE = re.compile(
    r'<div[^>]*class="[^"]*description[^"]*"[^>]*>(.*?)</div>',
    re.IGNORECASE | re.DOTALL,
)
ALT_DESCRIPTION_RE = re.compile(
    r'<div[^>]*class="[^"]*want-text[^"]*"[^>]*>(.*?)</div>',
    re.IGNORECASE | re.DOTALL,
)
# Секция с ценой — ищем ближайший блок к заголовку "Бюджет" или "Цена"
PRICE_SECTION_RE = re.compile(
    r"(?:Бюджет|Цена|Стоимость|Price|Budget)[^<]*?(?:от\s*)?(\d[\d\s]*)",
    re.IGNORECASE,
)


def check_kwork_criteria(project_title: str, project_description: str, criteria: list[str] | None = None) -> bool:
    """Check if a Kwork project matches criteria from KWORK_CRITERIA."""
    if not criteria:
        LOGGER.warning("KWORK_CRITERIA is empty — no criteria to match against")
        return False
    text = f"{project_title} {project_description}".lower()
    for criterion in criteria:
        if criterion in text:
            LOGGER.info("Kwork project matches criteria via: %s", criterion)
            return True
    return False


def load_profile(path: Path = PROFILE_PATH) -> str:
    """Load user profile from markdown file."""
    if not path.exists():
        LOGGER.warning("Profile file not found: %s", path)
        return ""
    return path.read_text(encoding="utf-8")


def extract_kwork_id(lead: LeadRecord, source: Source, message: Optional[Message] = None) -> Optional[str]:
    """Extract Kwork project ID from inline buttons (priority) or message text."""
    # 1. Сначала проверяем inline-кнопки (как просил пользователь)
    if message is not None:
        try:
            buttons = message.buttons
            if buttons:
                for row in buttons:
                    for btn in row:
                        if hasattr(btn, "url") and btn.url:
                            match = KWORK_PROJECT_RE.search(btn.url)
                            if match:
                                return match.group(1)
        except Exception as exc:
            LOGGER.warning("Failed to get message buttons: %s", exc)

    # 2. Fallback: ищем в тексте сообщения
    match = KWORK_PROJECT_RE.search(lead.text)
    if match:
        return match.group(1)

    return None


async def fetch_kwork_project(project_id: str) -> Optional[dict]:
    """Fetch Kwork project page and extract title, description, budget."""
    url = f"https://kwork.ru/projects/{project_id}"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    }

    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(url, headers=headers, follow_redirects=True, timeout=15.0)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            LOGGER.error("Failed to fetch Kwork project %s: %s", project_id, exc)
            return None

    html = resp.text

    # Title
    title_match = TITLE_RE.search(html)
    title = title_match.group(1).strip() if title_match else f"Project #{project_id}"
    title = re.sub(r"\s*[|–-]\s*Kwork\s*$", "", title, flags=re.IGNORECASE | re.MULTILINE).strip()

    # Description — ищем в различных контейнерах
    desc_match = DESCRIPTION_RE.search(html)
    if not desc_match:
        desc_match = ALT_DESCRIPTION_RE.search(html)
    description = ""
    if desc_match:
        raw = desc_match.group(1)
        description = re.sub(r"<[^>]+>", " ", raw)
        description = re.sub(r"\s+", " ", description).strip()[:3000]

    # Budget — многоуровневый поиск цены
    min_price = 5000  # fallback
    price_found = False

    # 1. Ищем цену в JSON-данных на странице (window.__INITIAL_STATE__ или script data)
    for match in re.finditer(r'"(?:price|cost|budget|order_cost_show)"\s*:\s*"?(\d+)"?', html):
        try:
            p = int(match.group(1))
            if 500 <= p <= 1000000:
                min_price = p
                price_found = True
                break
        except ValueError:
            pass

    # 2. Ищем в HTML-секции цены
    if not price_found:
        price_match = PRICE_SECTION_RE.search(html)
        if price_match:
            try:
                price = int(price_match.group(1).replace(" ", ""))
                if 500 <= price <= 1000000:
                    min_price = price
                    price_found = True
            except ValueError:
                pass

    # 3. Ищем прямые цены вида "80000 руб" (самый надёжный паттерн)
    if not price_found:
        all_prices = BUDGET_RE.findall(html)
        if all_prices:
            try:
                valid = [
                    int(p.replace(" ", "")) for p in all_prices[:15]
                    if 3 <= len(p.replace(" ", "")) <= 7
                ]
                valid = [p for p in valid if 500 <= p <= 1_000_000]
                if valid:
                    min_price = min(valid)
                    price_found = True
            except ValueError:
                pass

    # 4. Ищем диапазон цены (менее надёжно — часто находит даты)
    if not price_found:
        range_match = BUDGET_RANGE_RE.search(html)
        if range_match:
            try:
                price = int(range_match.group(1).replace(" ", ""))
                if 500 <= price <= 1000000:
                    min_price = price
                    price_found = True
            except ValueError:
                    pass

    LOGGER.debug(
        "Parsed project %s: title=%s, budget=%s, desc_len=%s",
        project_id, title[:40], min_price, len(description),
    )

    return {
        "id": project_id,
        "title": title,
        "description": description,
        "budget_min": min_price,
        "url": url,
    }


# Словарь замены английских технических терминов на русские аналоги
_RU_REPLACEMENTS = {
    # Технические термины (замена целых слов)
    r'\bAPI\b': 'программный интерфейс',
    r'\bJSON\b': 'формат данных',
    r'\bREST\b': 'веб-интерфейс',
    r'\bCRM\b': 'система управления клиентами',
    r'\bDocker\b': 'контейнеризация',
    r'\bbackend\b': 'серверная часть',
    r'\bfrontend\b': 'пользовательский интерфейс',
    r'\bTelegram\b': 'Телеграм',
    r'\bPython\b': 'Питон',
    r'\bSQLite\b': 'база данных',
    r'\bPostgreSQL\b': 'база данных',
    r'\bMySQL\b': 'база данных',
    r'\bRedis\b': 'хранилище данных',
    r'\bgit\b': 'система контроля версий',
    r'\bGitHub\b': 'репозиторий',
    r'\bFastAPI\b': 'веб-фреймворк',
    r'\bFlask\b': 'веб-фреймворк',
    r'\bDjango\b': 'веб-фреймворк',
    r'\bTelethon\b': 'библиотека',
    r'\bBitrix24\b': 'Битрикс24',
    r'\bNGINX\b': 'веб-сервер',
    r'\bNginx\b': 'веб-сервер',
    r'\bnginx\b': 'веб-сервер',
    r'\bRabbitMQ\b': 'очередь сообщений',
    r'\bHTML\b': 'разметка',
    r'\bCSS\b': 'стили',
    r'\bJavaScript\b': 'Джаваскрипт',
    r'\bTypeScript\b': 'Тайпскрипт',
    r'\bReact\b': 'библиотека',
    r'\bFunPay\b': 'торговая площадка',
    r'\bLzt\.market\b': 'торговая площадка',
    r'\b24/7\b': 'круглосуточно',
    r'\b24\s*/\s*7\b': 'круглосуточно',
    r'\b[Bb]ot\b': 'бот',
    r'\bDevOps\b': 'администрирование',
    r'\bNode\.?[Jj]s\b': 'Нод',
    r'\baiohttp\b': 'библиотека',
    r'\bbs4\b': 'библиотека',
    r'\bSelenium\b': 'библиотека',
    r'\bExcel\b': 'таблицы',
    r'Full[- ]?[Ss]tack': 'полный цикл',
    r'Backend': 'серверная часть',
    r'Frontend': 'пользовательский интерфейс',
    r'UI': 'интерфейс пользователя',
    r'proficient': 'опытен',
    r'PostgresSQL': 'база данных',
    r'Node': 'платформа',
    r'engine': 'движок',
}


_RU_REPLACEMENTS_RE = [
    (re.compile(pattern), replacement)
    for pattern, replacement in _RU_REPLACEMENTS.items()
]


def _russianize_text(text: str) -> str:
    """Replace English tech terms in generated text with Russian equivalents."""
    for pattern, replacement in _RU_REPLACEMENTS_RE:
        text = pattern.sub(replacement, text)
    # Fix common double-replacement artifacts
    text = text.replace('серверная часть часть', 'серверная часть')
    text = text.replace('серверная часть-часть', 'серверная часть')
    text = text.replace('пользовательский интерфейс интерфейс', 'пользовательский интерфейс')
    text = text.replace('база данных база данных', 'база данных')
    text = text.replace('библиотека библиотека', 'библиотека')
    # Strip greeting phrases
    text = re.sub(r'^(?:Примите мо[её]|Доброе утро|Добрый день|Здравствуйте|Привет)\s*', '', text, flags=re.IGNORECASE | re.MULTILINE)
    return text


async def generate_offer_text(
    project_title: str,
    project_description: str,
    profile_text: str,
    api_key: str = "",
    model: str = "openai/gpt-4o-mini",
) -> Optional[str]:
    """Generate offer body text via OpenRouter API. Returns only the body text."""
    if not api_key:
        LOGGER.error("OPENROUTER_API_KEY not configured")
        return None
    system_prompt = (
        "Ты — разработчик, который откликается на заказы.\n"
        "Используй информацию обо мне из сообщения пользователя — там написано,"
        " какой у меня опыт, стек, проекты.\n"
        "------\n"
        "СТИЛЬ: Пиши развёрнуто (1400-1700 символов), 4-5 абзацев, "
        "каждый абзац по 2-4 предложения. Не делай слишком короткие абзацы.\n"
        "Используй 2-3 emoji. В каждом абзаце используй факты из профиля.\n"
        "Тон: уверенный, деловой, без излишней сухости, с наглостью.\n"
        "------\n"
        "ЗАПРЕТЫ (строго): приветствия, обращения, имена, возраст, "
        "английские слова, названия библиотек и технологий"
    )

    user_prompt = (
        f"Название заказа: {project_title}\n"
        f"Описание: {project_description}"
        f"\n\nИнформация обо мне:\n{profile_text}\n"
        "\n---\n"
        "Напиши отклик на этот заказ. Используй информацию обо мне.\n"
        "\nТРЕБОВАНИЯ:\n"
        "- 4-5 абзацев, каждый по 2-4 предложения. Не делай абзацы из 1 предложения.\n"
        "- Длина: 1400-1700 символов\n"
        "- 2-3 emoji (🚀 💻 🎯 🔥 ✅ ⚡)\n"
        "- В каждом абзаце — конкретный факт из информации обо мне\n"
        "- Объясни, почему именно я подхожу для этой задачи\n"
        "- Только русский язык, без английских слов\n"
        "- Без приветствий, имени, возраста\n"
        "- Без упоминания библиотек, языков, технологий\n"
        "- Отправь только текст отклика"
        "- Получатель отлкика не знает обо мне ничего, в отлике напиши информацию обо мне которая будет важна этому заказчику"
    )

    try:
        with OpenRouter(
        api_key=api_key,
        ) as client:
            response = client.chat.send(
                model=model,
                messages=[
                {
                    "role": "system",
                    "content": system_prompt
                },
                {
                    "role": "user",
                    "content": user_prompt
                }
                ]
            )
        return str(response.choices[0].message.content)
    except Exception as e:
        return None


def _generate_boundary() -> str:
    """Generate a random multipart boundary."""
    chars = string.ascii_letters + string.digits
    return "----WebKitFormBoundary" + "".join(random.choices(chars, k=16))


async def send_kwork_offer(
    project_id: str,
    title: str,
    description: str,
    price: int,
    csrf_token: str,
    rorssqihek: str = "",
) -> bool:
    """Send offer to Kwork via API."""
    if not csrf_token:
        LOGGER.error("CSRF_USER_TOKEN not configured in .env")
        return False

    boundary = _generate_boundary()

    def _part(name: str, value: str) -> str:
        # Экранируем \r\n чтобы не сломать multipart boundary
        safe_value = value.replace("\r\n", "\n").replace("\r", "")
        return (
            f"--{boundary}\r\n"
            f"Content-Disposition: form-data; name=\"{name}\"\r\n\r\n"
            f"{safe_value}\r\n"
        )

    body = ""
    body += _part("wantId", project_id)
    body += _part("offerType", "custom")
    body += _part("description", description)
    body += _part("kwork_duration", "2")  # всегда 2 дня
    body += _part("kwork_price", str(price))
    body += _part("kwork_name", f"<div>{title}</div>")
    body += f"--{boundary}--\r\n"

    # Формируем cookies: csrf_user_token + RORSSQIHEK (если есть)
    cookies = f"csrf_user_token={csrf_token}"
    if rorssqihek:
        cookies += f"; RORSSQIHEK={rorssqihek}"

    headers = {
        "accept": "application/json, text/plain, */*",
        "accept-language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        "content-type": f"multipart/form-data; boundary={boundary}",
        "sec-ch-ua": '"Google Chrome";v="143", "Chromium";v="143", "Not A(Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "x-requested-with": "XMLHttpRequest",
        "x-csrf-token": csrf_token,
        "cookie": cookies,
        "referer": f"https://kwork.ru/new_offer?project={project_id}",
    }

    async with httpx.AsyncClient() as client:
        try:
            resp = await client.post(
                "https://kwork.ru/api/offer/createoffer",
                headers=headers,
                content=body.encode("utf-8"),
                timeout=30.0,
            )
            body_text = resp.text[:1000]
            LOGGER.info("Kwork API response [%s]: %s", resp.status_code, body_text)
            # Kwork всегда возвращает HTTP 200, успех в JSON-поле "success"
            try:
                resp_json = json.loads(resp.text)
                return bool(resp_json.get("success"))
            except json.JSONDecodeError:
                return resp.is_success
        except httpx.HTTPError as exc:
            LOGGER.error("Failed to send Kwork offer: %s", exc)
            return False



