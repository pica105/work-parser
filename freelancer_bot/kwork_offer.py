from __future__ import annotations

import json
import logging
import random
import re
import string
from pathlib import Path
from typing import Optional

import httpx
from telethon.tl.custom.message import Message

from .sources import Source
from .storage import LeadRecord

LOGGER = logging.getLogger("freelancer_bot.kwork")


# Regex для извлечения цен из текста сообщения
# Поддерживает: 30,000 ₽, 15000 руб, 50 000 ₽, 1200$
KWORK_PRICE_RE = re.compile(r"(\d[\d,\s]*)\s*(?:₽|руб|\$|eur|usd)", re.IGNORECASE)
# Fallback: ищем число после слов Бюджет/От/Цена (без валюты)
KWORK_FALLBACK_RE = re.compile(
    r"(?:Бюджет|Цена|Стоимость|Price|Budget|От|от)[:\s]*(\d[\d,\s]*)", re.IGNORECASE
)
# Паттерн для диапазона: "От 30,000 ₽ до 90,000 ₽", "Бюджет: 15000 - 30000", "1000-2000"
KWORK_RANGE_RE = re.compile(
    r"(?:(?:От|от)\s*)?(\d[\d,\s]*)(?:\s*(?:₽|руб|\$))?\s*(?:до|-|–|—)\s*(\d[\d,\s]*)",
    re.IGNORECASE,
)


def _parse_price(raw: str) -> int | None:
    """Очищает строку цены от разделителей и парсит в int."""
    cleaned = raw.replace(",", "").replace(" ", "")
    if cleaned and cleaned.isdigit():
        return int(cleaned)
    return None


def extract_kwork_price(text: str) -> int:
    """Извлекает первую цену из текста сообщения Kwork.
    Ищет число перед ₽/руб (приоритетно), затем числа рядом со словом Бюджет/От.
    """
    # 1. Ищем первое число перед валютой (₽, руб, $)
    match = KWORK_PRICE_RE.search(text)
    if match:
        price = _parse_price(match.group(1))
        if price and 100 <= price <= 10_000_000:
            return price

    # 2. Fallback: ищем число после слов Бюджет/От/Цена (даже без валюты)
    match = KWORK_FALLBACK_RE.search(text)
    if match:
        price = _parse_price(match.group(1))
        if price and 100 <= price <= 10_000_000:
            return price

    return 5000  # fallback


def extract_kwork_price_range(text: str) -> tuple[int, int]:
    """Извлекает минимальную и максимальную цену из сообщения Kwork.
    Возвращает (min_price, max_price).
    Приоритет: диапазон "От X до Y" → все найденные цены → fallback 5000.
    """
    # 1. Ищем явный диапазон "От X до Y", "X - Y" или "X- Y"
    range_match = KWORK_RANGE_RE.search(text)
    if range_match:
        min_p = _parse_price(range_match.group(1))
        max_p = _parse_price(range_match.group(2))
        if min_p and max_p and min_p > 0 and max_p > 0:
            return (min(min_p, max_p), max(min_p, max_p))

    # 2. Ищем все цены с валютой через finditer
    prices = []
    seen = set()
    for match in KWORK_PRICE_RE.finditer(text):
        p = _parse_price(match.group(1))
        if p and 100 <= p <= 10_000_000 and p not in seen:
            prices.append(p)
            seen.add(p)

    # 3. Если < 2 цен — добавляем числа после Бюджет/От/Цена
    if len(prices) < 2:
        for match in KWORK_FALLBACK_RE.finditer(text):
            p = _parse_price(match.group(1))
            if p and 100 <= p <= 10_000_000 and p not in seen:
                prices.append(p)
                seen.add(p)

    if len(prices) >= 2:
        return (min(prices), max(prices))
    elif len(prices) == 1:
        return (prices[0], prices[0])

    return (5000, 5000)  # fallback


def calc_kwork_final_price(min_price: int, max_price: int) -> int:
    """Рассчитывает финальную цену отклика:
    - Берёт минимальную цену + 10% от максимальной
    - Если результат превышает максимальный бюджет заказчика — использует минимальную
    """
    final = min_price + max_price // 10
    if final > max_price:
        return min_price
    return final

PROFILE_PATH = Path("profile.md")

# Kwork project ID from URL like https://kwork.ru/projects/3175656?ref=...
KWORK_PROJECT_RE = re.compile(r"kwork\.ru/projects/(\d+)")

# Budget patterns — ищем в блоке с ценой проекта (только для fetch_kwork_project)
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
    ai_basic_url: str = "https://openrouter.ai/api/v1/chat/completions",
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
        "До 10 emoji. В каждом абзаце используй факты из профиля.\n"
        "Тон: уверенный, деловой, без излишней сухости, с наглостью.\n"
        "------\n"
        "ЗАПРЕТЫ (строго): приветствия, обращения, имена, возраст, "
        "английские слова, названия библиотек и технологий, литературная лексика"
    )

    user_prompt = (
        f"Название заказа: {project_title}\n"
        f"Описание заказа:\n{project_description}\n\n"
        f"Информация обо мне:\n{profile_text}\n"
        "\n---\n"
        "Напиши отклик так, будто опытный разработчик пишет заказчику в Telegram или личные сообщения.\n"
        "Текст должен выглядеть живым, немного грубоватым и естественным.\n"
        "Главная цель — чтобы текст не выглядел как генерация нейросети.\n\n"

        "КАК НУЖНО ПИСАТЬ:\n"
        "- Короткие и средние предложения\n"
        "- Простые слова\n"
        "- Разговорный стиль\n"
        "- Уверенный тон\n"
        "- Иногда можно писать немного резко или прямолинейно\n"
        "- Текст должен ощущаться как сообщение от реального разраба\n"
        "- Допустимы неполные предложения\n"
        "- Иногда можно использовать слова вроде: 'скорее всего', 'обычно', 'походу', 'тут проблема в...'\n"
        "- Пиши так, будто уже сталкивался с подобной проблемой\n"
        "- Допустима легкая небрежность в формулировках\n"
        "- Иногда можно использовать разговорные слова: 'костыли', 'поехало', 'сломано', 'упирается в', 'надо смотреть'\n"
        "- Не делай текст идеально вылизанным\n"
        "- Разрешены слегка кривые или короткие фразы, если звучит естественно\n\n"

        "ЧЕГО НЕ ДЕЛАТЬ:\n"
        "- Не писать как коммерческое предложение\n"
        "- Не использовать пафос\n"
        "- Не использовать канцелярит\n"
        "- Не использовать длинные сложные предложения\n"
        "- Не использовать абстрактные фразы\n"
        "- Не использовать слишком умные или официальные слова\n"
        "- Не писать как HR, менеджер или копирайтер\n"
        "- Не делать идеально логичную структуру текста\n"
        "- Не объясняй очевидные вещи\n"
        "- Не повторяй мысли разными словами\n"
        "- Не пиши слишком грамотно и стерильно\n"
        "- Не используй фразы вроде:\n"
        "  'уверенность в своих силах'\n"
        "  'задачи любой сложности'\n"
        "  'отправная точка'\n"
        "  'бизнес-процессы'\n"
        "  'успешно завершенные проекты'\n"
        "  'буду рад сотрудничеству'\n"
        "  'идеальное решение'\n"
        "  'готов помочь'\n"
        "  'мой опыт позволяет'\n"
        "  'я специализируюсь'\n"
        "  'имею понимание'\n"
        "  'эффективное решение'\n"
        "  'готов взяться'\n"
        "- Не использовать фразы, которые звучат как мотивационная речь\n"
        "- Не писать выводы в стиле 'давай разберемся', 'починим как надо', 'будет интересно сотрудничать'\n"
        "- Не пытаться заканчивать текст красиво\n"
        "- Не делать плавные литературные переходы между абзацами\n"
        "- Не использовать фразы вроде:\n"
        "  'есть большой шанс'\n"
        "  'не дико новая тема для меня'\n"
        "  'всегда приятно работать'\n"
        "  'понимаю важность качества'\n"
        "  'сможем решить'\n"
        "  'предложить оптимизации'\n"
        "- Не использовать emoji если они не выглядят естественно\n"
        "- Последний абзац не должен выглядеть как продажа услуг\n"
        "- Последний абзац должен быть коротким и приземленным\n"
        "  'накопилось достаточно опыта'\n\n"


        "ЧТО ДОЛЖНО БЫТЬ В ТЕКСТЕ:\n"
        "- Понимание проблемы заказчика\n"
        "- Краткое предположение причины бага или подхода к фиксу\n"
        "- Релевантный опыт\n"
        "- Почему я подхожу под задачу\n"
        "- Если уместно — упоминание похожих задач\n"
        "- Последний абзац должен звучать спокойно и буднично, без попытки продать себя\n"
        "- Хотя бы одно предложение должно звучать как мысль, написанная на ходу\n"
        "- Допустима легкая разговорная кривизна, если текст звучит живее\n"
        "- Хотя бы одна мысль должна звучать как личное наблюдение разработчика\n\n"

        "ФОРМАТ:\n"
        "- 2-4 абзаца\n"
        "- Общая длина 600-1000 символов\n"
        "- Без приветствий\n"
        "- Без списков\n"
        "- Только русский язык\n"
        "- Без английских слов по возможности\n"
        "- Emoji максимум 0-1\n"
        "- Не использовать emoji просто так\n\n"

        "ПРИМЕР ХОРОШЕГО ТОНА:\n"
        "'Тут походу проблема либо в логике сравнения инвентаря, либо старые предметы нормально не чистятся после повторного прохода.'\n"
        "'Со steam inventory уже работал, там часто всплывают проблемы с кешем и таймингами.'\n"
        "'Если парсер старый — скорее всего там уже накопились костыли, поэтому надо смотреть всю цепочку обновления инвентаря.'\n"
        "'Обычно такие баги сидят не в самом парсинге, а в том как хранится прошлое состояние инвентаря.'\n\n"

        "ВАЖНО:\n"
        "- Текст должен выглядеть как быстро написанное сообщение человека, а не как заранее подготовленный отклик\n"
        "- Лучше немного грубо и просто, чем слишком красиво\n"
        "- Если предложение можно упростить — упрощай\n"
        "- Убирай объяснения очевидных вещей (не пиши 'обычно важно', 'как правило', 'по сути')\n"
        "- Пиши короче: меньше связок между предложениями\n"
        "- Не используй переходные фразы ('по сути', 'к тому же', 'в целом', 'если решим')\n"
        "- Последний абзац НЕ должен продавать услуги — только техническое завершение или наблюдение\n"
        "- Не пытайся впечатлить заказчика красивыми словами\n\n"

        "Отправь только готовый текст отклика."
    )

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    payload["provider"] = {
        "order": ["Azure"],
        "allow_fallbacks": True,
    }
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                url=ai_basic_url,
                headers={
                    "Authorization": f"Bearer {api_key.strip()}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=60.0,
            )
            response.raise_for_status()  # кидает ошибку если status != 200
            data = response.json()
            return data["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"Ошибка: {e}")
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



