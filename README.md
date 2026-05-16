# Freelance Lead Bot

Мониторинг 28 Telegram-каналов с фриланс-заказами. Фильтрация по ключевым словам, автоотклики на Kwork через ИИ с подтверждением в один клик.

## Архитектура

```
Telegram-каналы (28 шт.)
     ↓  Telethon (user_client)
Обработка сообщений
     ├── @freelance_dev_work (Kwork):
     │   ├── Парсинг страницы проекта
     │   ├── Проверка критериев backend/API
     │   ├── Генерация отклика через OpenRouter
     │   └── Уведомление в Telegram с inline-кнопкой «Авто-отклик»
     ├── Остальные каналы:
     │   └── Фильтр по ключевым словам (scoring)
     └── Отправка админу через bot_client
```

Два Telegram-клиента:
- **user_client** — личный аккаунт (читает каналы)
- **bot_client** — бот (команды, уведомления, inline-кнопки)

## Конфигурация

Все настройки — в `.env`. Обязательные поля:

| Переменная | Описание |
|---|---|
| `TELEGRAM_API_ID` | Из [my.telegram.org/apps](https://my.telegram.org/apps) |
| `TELEGRAM_API_HASH` | Оттуда же |
| `TELEGRAM_BOT_TOKEN` | От [@BotFather](https://t.me/BotFather) |
| `ADMIN_TELEGRAM_ID` | Твой ID (узнать — @userinfobot) |

Опционально (для Kwork):

| Переменная | Описание |
|---|---|
| `OPENROUTER_API_KEY` | Ключ из [openrouter.ai/keys](https://openrouter.ai/keys) |
| `OPENROUTER_MODEL` | Модель, по умолчанию `openai/gpt-4o-mini` |
| `CSRF_USER_TOKEN` | CSRF-токен с kwork.ru (Cookies → `csrf_user_token`) |
| `KWORK_AUTH_COOKIES` | Куки авторизации Kwork (формат: `uad=...; RORSSQIHEK=...`) |
| `KWORK_CRITERIA` | Критерии отбора проектов через запятую (см. Kwork: pipeline) |

## Команды бота

Работают только у администратора (по `ADMIN_TELEGRAM_ID`):

| Команда | Описание |
|---|---|
| `/status` | Статистика: каналы, лиды, черновики Kwork |
| `/sources` | Список отслеживаемых каналов |
| `/keywords` | Ключевые и стоп-слова |
| `/test текст` | Проверить текст через фильтр |

## Kwork: pipeline

1. Приходит сообщение из @freelance_dev_work
2. Бот парсит страницу проекта на Kwork (название, описание, бюджет)
3. Проверка по критериям из `KWORK_CRITERIA` в `.env` (формат: слова/фразы через запятую)
4. **Не подходит** → простое уведомление (название + ссылка)
5. **Подходит** → OpenRouter генерирует отклик на основе `profile.md`
6. Отклик сохраняется в БД (`kwork_offers`), приходит уведомление с кнопкой
7. **Нажатие кнопки** → отклик отправляется на Kwork API

Отклик никогда не отправляется автоматически — только по нажатию кнопки.

## Фильтрация

- **`filters_config/keywords.json`** — слова для поиска с весами. Формат:
  ```json
  { "Категория": { "слово": 3, "другое": 2 } }
  ```
- **`filters_config/stop_words.json`** — стоп-слова (массив строк)
- Порог: score >= 2 для пропуска

## Установка и запуск

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# заполнить .env
python3 -m freelancer_bot
```

Подробнее — [QUICK_START.md](./QUICK_START.md).

## Структура проекта

```
freelancer_bot/
├── app.py            # Ядро: LeadBot, CLI, очередь, обработчики
├── config.py         # RuntimeConfig — загрузка .env
├── filters.py        # Скоринг по ключевым словам
├── kwork_offer.py    # Kwork: парсинг, генерация, отправка
├── storage.py        # SQLite: лиды, подписчики, черновики
├── sources.py        # 28 Telegram-каналов
├── formatting.py     # Форматирование уведомлений
├──    filters_config/   # JSON-файлы фильтров
│   ├── keywords.json
│   └── stop_words.json
├── __init__.py       # Версия
└── __main__.py       # Точка входа
```
