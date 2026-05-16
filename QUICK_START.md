# Быстрый старт

## 1. Установка

```bash
# Требования: Python 3.10+
python3 -m venv .venv
source .venv/bin/activate          # Linux/Mac
# .venv\Scripts\activate           # Windows

pip install -r requirements.txt
cp .env.example .env
```

## 2. Что нужно заполнить в `.env`

| Переменная | Где взять |
|---|---|
| `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` | [my.telegram.org/apps](https://my.telegram.org/apps) |
| `TELEGRAM_BOT_TOKEN` | [@BotFather](https://t.me/BotFather) → `/newbot` |
| `ADMIN_TELEGRAM_ID` | [@userinfobot](https://t.me/userinfobot) — твой ID |

```ini
TELEGRAM_API_ID=12345
TELEGRAM_API_HASH=abc123def456
TELEGRAM_BOT_TOKEN=123456:ABC-...
ADMIN_TELEGRAM_ID=123456789
```

## 3. Запуск

```bash
source .venv/bin/activate          # Linux/Mac
# .venv\Scripts\activate           # Windows
python3 -m freelancer_bot
```

При первом запуске Telethon запросит номер телефона и код из Telegram (аккаунт, который читает каналы). Данные хранятся локально в `sessions/`.

## 4. Проверка фильтра без запуска бота

```bash
python3 -m freelancer_bot --check-filter "нужен телеграм бот на Python"
```

## 5. Настройка Kwork

```ini
# В .env:
KWORK_CRITERIA=backend,api,rest,server,telegram bot,бэкенд,апи,серверная,микросервис,тг бот
OPENROUTER_API_KEY=твой_ключ_из_openrouter
CSRF_USER_TOKEN=...
KWORK_AUTH_COOKIES=uad=...; RORSSQIHEK=...
# и создай profile.md из profile.example.md со своими данными
```

- `KWORK_CRITERIA` — слова/фразы для отбора проектов (через запятую)
- `profile.md` — информация о тебе для генерации откликов

Подробнее о CSRF и куках — в README.md → «Kwork: pipeline».
