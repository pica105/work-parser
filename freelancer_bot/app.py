from __future__ import annotations

import argparse
import asyncio
import html
import logging
import os
import signal
import sqlite3
import time
from datetime import datetime, timezone, timedelta
from typing import Iterable, Optional
from dotenv import load_dotenv

from telethon import TelegramClient, events, Button
from telethon.errors import RPCError
from telethon.tl.custom.message import Message

from .config import RuntimeConfig
from .filters import KEYWORDS, STOP_WORDS, match_text
from .formatting import format_lead
MSK = timezone(timedelta(hours=3))


def _prepare_sqlite_db(session_path: os.PathLike) -> None:
    """Clean stale lock files and enable WAL mode for a Telethon SQLite session."""
    base = str(session_path)
    # Telethon использует .sqlite или .session расширение - пробуем оба
    candidates = [base + '.sqlite', base + '.session', base]
    db_path = next((p for p in candidates if os.path.isfile(p)), None)
    if not db_path:
        db_path = base + '.sqlite'
    # Удаляем stale lock-файлы от предыдущих крашей (WAL и rollback journal)
    for suffix in ('-wal', '-shm', '-journal'):
        stale = db_path + suffix
        if os.path.isfile(stale):
            try:
                os.unlink(stale)
            except OSError:
                pass
    # Включаем WAL режим и таймаут для устойчивости к конкурентному доступу
    try:
        conn = sqlite3.connect(db_path, timeout=10)
        conn.execute('PRAGMA journal_mode=WAL;')
        conn.execute('PRAGMA synchronous=NORMAL;')
        conn.execute('PRAGMA busy_timeout=5000;')
        conn.close()
    except sqlite3.Error:
        pass


def _msk_timestamp(dt: Optional[datetime]) -> str:
    """Convert a naive UTC datetime or None to MSK ISO string."""
    if dt is None:
        return datetime.now(MSK).isoformat()
    return dt.replace(tzinfo=timezone.utc).astimezone(MSK).isoformat()


from .sources import Source, enabled_sources
from .storage import LeadRecord, Storage
from .kwork_offer import (
    extract_kwork_id,
    fetch_kwork_project,
    check_kwork_criteria,
    generate_offer_text,
    send_kwork_offer,
    load_profile,
)


LOGGER = logging.getLogger("freelancer_bot")


class LeadBot:
    def __init__(self, config: RuntimeConfig):
        self.config = config
        config.user_session_path.parent.mkdir(parents=True, exist_ok=True)
        config.bot_session_path.parent.mkdir(parents=True, exist_ok=True)
        # Очищаем stale lock-файлы и включаем WAL режим для сессий Telethon
        _prepare_sqlite_db(config.user_session_path)
        _prepare_sqlite_db(config.bot_session_path)
        self.storage = Storage(config.database_path)
        self.sources = enabled_sources()
        self.user_client = TelegramClient(
            str(config.user_session_path),
            config.api_id,
            config.api_hash,
        )
        self.bot_client = TelegramClient(
            str(config.bot_session_path),
            config.api_id,
            config.api_hash,
        )
        # Админ — единственный получатель уведомлений
        if not config.admin_telegram_id:
            raise RuntimeError(
                "❌ ADMIN_TELEGRAM_ID не указан в .env\n"
                "Укажи Telegram ID администратора, например: ADMIN_TELEGRAM_ID=123456789"
            )
        self._admin_chat_id = config.admin_telegram_id
        # Очередь для отправки лидов
        self._queue: asyncio.Queue[tuple[LeadRecord, Source]] = asyncio.Queue()
        self._running = False
        self._csrf_token = config.csrf_user_token
        self._kwork_auth_cookies = config.kwork_auth_cookies
        self._kwork_handle = "@freelance_dev_work"
        self._openrouter_api_key = config.openrouter_api_key
        self._openrouter_model = config.openrouter_model
        self._kwork_offered: set[str] = set()

    async def run(self) -> None:
        self._register_bot_commands()

        print()
        print("╔══════════════════════════════════════════╗")
        print("║  🔐 Необходим вход в Telegram           ║")
        print("║                                         ║")
        print("║  Бот использует твой аккаунт для        ║")
        print("║  чтения каналов с заказами.             ║")
        print("║                                         ║")
        print("║  Введи номер телефона (как при входе    ║")
        print("║  в Telegram). Код придёт в приложение.  ║")
        print("╚══════════════════════════════════════════╝")
        print()
        await self.user_client.start()
        await self.bot_client.start(bot_token=self.config.bot_token)

        # Добавляем админа как получателя уведомлений
        self.storage.add_subscriber(self._admin_chat_id)

        active_sources = await self._register_source_handlers()
        LOGGER.info("Monitoring %s Telegram sources", len(active_sources))

        if self.config.send_catch_up and self.config.catch_up_limit > 0:
            await self._catch_up(active_sources)

        # Запускаем фоновую задачу отправки с очередью
        self._running = True
        sender_task = asyncio.create_task(self._sender_loop())

        await self._wait_until_stopped()
        self._running = False
        sender_task.cancel()
        try:
            await sender_task
        except asyncio.CancelledError:
            pass

    async def shutdown(self) -> None:
        self._running = False
        await self.user_client.disconnect()
        await self.bot_client.disconnect()
        self.storage.close()

    async def _handle_kwork_project(self, source: Source, message: Message, text: str) -> None:
        """
        Обрабатывает Kwork-проект:
        1. Извлекает ID проекта
        2. Получает данные со страницы Kwork
        3. Проверяет на соответствие критериям backend/API
        4. Если подходит — генерирует отклик, сохраняет черновик и отправляет уведомление с кнопкой
        5. Если не подходит — отправляет простое уведомление
        """
        if not self._csrf_token:
            LOGGER.warning("CSRF_USER_TOKEN not set — Kwork offers disabled")

        # Создаём LeadRecord для базовой записи в БД
        link = f"https://t.me/{source.username}/{message.id}"
        message_date = _msk_timestamp(message.date)
        lead = LeadRecord(
            source=source.handle,
            message_id=int(message.id),
            link=link,
            text=text,
            score=1,
            keywords=("_kwork",),
            message_date=message_date,
        )

        # 1. Извлекаем ID проекта Kwork
        project_id = extract_kwork_id(lead, source, message)
        if not project_id:
            LOGGER.info("No Kwork project ID found in message from %s", source.handle)
            return

        LOGGER.info("Kwork project detected: %s", project_id)

        # Дедупликация: уже обрабатывали этот проект
        if project_id in self._kwork_offered:
            LOGGER.info("Already processed Kwork project %s, skipping", project_id)
            return
        self._kwork_offered.add(project_id)

        # Проверяем, есть ли уже черновик в БД (от предыдущего запуска)
        existing_draft = self.storage.get_kwork_offer_draft(project_id)
        if existing_draft:
            LOGGER.info("Found existing draft for project %s", project_id)
            await self._send_kwork_notification_with_button(
                project_id, existing_draft["title"],
                existing_draft["offer_text"],
                existing_draft["price"],
            )
            return

        # 2. Получаем данные со страницы Kwork
        project = await fetch_kwork_project(project_id)
        if not project:
            LOGGER.warning("Could not fetch Kwork project %s, sending simple notification", project_id)
            await self._send_simple_kwork_notification(project_id, f"Project #{project_id}", 0)
            return

        title = project["title"]
        description = project["description"]
        price = project["budget_min"]

        # 3. Проверяем на соответствие критериям backend/API
        if not check_kwork_criteria(title, description, criteria=self.config.kwork_criteria):
            LOGGER.info("Kwork project %s does not match backend/API criteria, sending simple notification", project_id)
            await self._send_simple_kwork_notification(project_id, title, price)
            return

        LOGGER.info("Kwork project %s matches criteria, generating offer...", project_id)

        # 4. Генерируем отклик через OpenRouter
        profile = load_profile()
        if not profile:
            LOGGER.warning("Profile is empty, cannot generate offer")
            await self._send_simple_kwork_notification(project_id, title, price)
            return

        offer_text = await generate_offer_text(
            title,
            description,
            profile,
            api_key=self._openrouter_api_key,
            model=self._openrouter_model,
        )
        if not offer_text:
            LOGGER.warning("Failed to generate offer text for project %s", project_id)
            await self._send_simple_kwork_notification(project_id, title, price)
            return

        # 5. Сохраняем черновик в БД
        self.storage.save_kwork_offer_draft(project_id, title, description, price, offer_text)
        LOGGER.info("Saved offer draft for project %s", project_id)

        # 6. Отправляем уведомление с кнопкой
        await self._send_kwork_notification_with_button(project_id, title, offer_text, price)

    async def _send_simple_kwork_notification(self, project_id: str, title: str, price: int) -> None:
        """Отправляет простое уведомление о Kwork-проекте (без отклика)."""
        text = (
            f"📋 <b>Kwork-проект (не подходит под критерии)</b>\n"
            f"Проект: <b>{html.escape(title[:100])}</b>\n"
            f"Бюджет: <b>{price} руб.</b>\n"
            f"<a href=\"https://kwork.ru/projects/{project_id}/view\">Открыть на Kwork</a>"
        )
        try:
            await self.bot_client.send_message(
                self._admin_chat_id,
                text,
                parse_mode="html",
                link_preview=False,
            )
        except Exception as send_err:
            LOGGER.warning("Failed to send simple Kwork notification: %s", send_err)

    async def _send_kwork_notification_with_button(self, project_id: str, title: str, offer_text: str, price: int) -> None:
        """Отправляет уведомление о Kwork-проекте с inline-кнопкой 'авто-отклик'."""
        text = (
            f"📋 <b>Kwork-проект (подходит под критерии)</b>\n"
            f"ID: <code>{project_id}</code>\n"
            f"Проект: <b>{html.escape(title[:100])}</b>\n"
            f"Бюджет: <b>{price} руб.</b>\n\n"
            f"<b>Сгенерированный отклик:</b>\n{html.escape(offer_text)}\n\n"
            f"<a href=\"https://kwork.ru/projects/{project_id}/view\">Открыть на Kwork</a>"
        )
        buttons = [[Button.inline("🤖 Авто-отклик", data=f"kwork:{project_id}")]]
        try:
            await self.bot_client.send_message(
                self._admin_chat_id,
                text,
                parse_mode="html",
                link_preview=False,
                buttons=buttons,
            )
            LOGGER.info("Sent Kwork notification with button for project %s", project_id)
        except Exception as send_err:
            LOGGER.warning("Failed to send Kwork notification with button: %s", send_err)

    async def _sender_loop(self) -> None:
        """Фоновая задача: накапливает лиды, сортирует по score, отправляет с задержкой."""
        buffer: list[tuple[LeadRecord, Source]] = []

        while self._running:
            # Ждём первый элемент
            try:
                item = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                buffer.append(item)
            except asyncio.TimeoutError:
                continue

            last_arrival = time.monotonic()

            # Накапливаем, пока поступают новые сообщения
            while True:
                elapsed = time.monotonic() - last_arrival
                # Чем больше элементов в буфере, тем дольше ждём для накопления
                if len(buffer) > 2:
                    settle_timeout = max(0, 2.0 - elapsed)
                else:
                    settle_timeout = max(0, 0.8 - elapsed)

                if settle_timeout <= 0:
                    break

                try:
                    item = await asyncio.wait_for(self._queue.get(), timeout=settle_timeout)
                    buffer.append(item)
                    last_arrival = time.monotonic()
                except asyncio.TimeoutError:
                    break  # Поток иссяк — пора отправлять

            # Сортируем от меньшего score к большему
            buffer.sort(key=lambda x: x[0].score)

            LOGGER.info(
                "Sending %s leads (scores: %s)",
                len(buffer),
                [item[0].score for item in buffer],
            )

            for lead, source in buffer:
                await self._send_lead(source, lead)
                await asyncio.sleep(0.5)

            buffer.clear()

    async def _send_lead(self, source: Source, lead: LeadRecord) -> None:
        """Отправляет лид админу."""
        body = format_lead(source, lead)
        try:
            await self.bot_client.send_message(
                self._admin_chat_id, body, parse_mode="html", link_preview=False
            )
            self.storage.mark_notified(lead.source, lead.message_id)
            LOGGER.info("Delivered lead from %s message %s", source.handle, lead.message_id)
        except RPCError as exc:
            LOGGER.warning("Could not deliver lead: %s", exc)

    def _register_bot_commands(self) -> None:
        def _is_admin(event) -> bool:
            return int(event.chat_id) == self._admin_chat_id

        @self.bot_client.on(events.NewMessage(pattern=r"^/status"))
        async def status(event: events.NewMessage.Event) -> None:
            if not _is_admin(event):
                return
            stats = self.storage.stats()
            await event.respond(
                "Статус:\n"
                f"- источников: {len(self.sources)}\n"
                f"- лидов в базе: {stats['leads']}\n"
                f"- черновиков Kwork: {stats['kwork_drafts']}\n"
                f"- ожидают повторной отправки: {stats['pending']}"
            )

        @self.bot_client.on(events.NewMessage(pattern=r"^/sources"))
        async def sources(event: events.NewMessage.Event) -> None:
            if not _is_admin(event):
                return
            lines = [f"{index}. {source.handle} — {source.title}" for index, source in enumerate(self.sources, 1)]
            await event.respond("Активные источники:\n" + "\n".join(lines))

        @self.bot_client.on(events.NewMessage(pattern=r"^/keywords"))
        async def keywords(event: events.NewMessage.Event) -> None:
            if not _is_admin(event):
                return
            keyword_preview = ", ".join(list(KEYWORDS.keys())[:35])
            stop_preview = ", ".join(STOP_WORDS[:35])
            await event.respond(
                "Ключевые слова:\n"
                f"{keyword_preview}\n\n"
                "Стоп-слова:\n"
                f"{stop_preview}"
            )

        @self.bot_client.on(events.NewMessage(pattern=r"^/test(?:\\s+(.+))?"))
        async def test_filter(event: events.NewMessage.Event) -> None:
            if not _is_admin(event):
                return
            text = event.pattern_match.group(1)
            if not text:
                await event.respond("Пришли так: /test нужен телеграм бот на Python")
                return

            result = match_text(text)
            if result.accepted:
                await event.respond(
                    f"Пройдет фильтр. Score: {result.score}. Совпало: {', '.join(result.matched_keywords)}"
                )
            else:
                reason = (
                    f"стоп-слова: {', '.join(result.rejected_by)}"
                    if result.rejected_by
                    else f"score ниже порога: {result.score}"
                )
                await event.respond(f"Не пройдет фильтр: {reason}")

        # Регистрируем обработчик inline-кнопок
        @self.bot_client.on(events.CallbackQuery)
        async def on_kwork_callback(event: events.CallbackQuery.Event) -> None:
            if int(event.chat_id) != self._admin_chat_id:
                await event.answer("Это не твоя кнопка", alert=True)
                return

            data = event.data.decode()
            if not data.startswith("kwork:"):
                return

            project_id = data[6:]
            await event.answer("Отправляю отклик...", alert=False)

            # Получаем черновик из БД
            draft = self.storage.get_kwork_offer_draft(project_id)
            if not draft:
                await event.edit(
                    event.message.text + "\n\n❌ <b>Черновик отклика не найден в БД</b>",
                    parse_mode="html",
                    buttons=None,
                )
                return

            # Отправляем отклик на Kwork
            LOGGER.info("Sending Kwork offer for project %s...", project_id)
            success = await send_kwork_offer(
                project_id=project_id,
                title=draft["title"],
                description=draft["offer_text"],
                price=draft["price"],
                csrf_token=self._csrf_token,
                auth_cookies=self._kwork_auth_cookies,
            )

            if success:
                # Удаляем черновик из БД
                self.storage.delete_kwork_offer_draft(project_id)
                await event.edit(
                    event.message.text + "\n\n✅ <b>Отклик успешно отправлен на Kwork!</b>",
                    parse_mode="html",
                    buttons=None,
                )
                LOGGER.info("Successfully sent Kwork offer for project %s", project_id)
            else:
                await event.edit(
                    event.message.text + "\n\n❌ <b>Ошибка отправки отклика на Kwork</b>",
                    parse_mode="html",
                    buttons=None,
                )
                LOGGER.error("Failed to send Kwork offer for project %s", project_id)

    async def _register_source_handlers(self) -> list[tuple[Source, object]]:
        active: list[tuple[Source, object]] = []
        for source in self.sources:
            try:
                entity = await self.user_client.get_entity(source.handle)
            except (ValueError, RPCError) as exc:
                LOGGER.warning("Could not resolve %s: %s", source.handle, exc)
                continue

            active.append((source, entity))

            @self.user_client.on(events.NewMessage(chats=entity))
            async def on_message(event: events.NewMessage.Event, source: Source = source) -> None:
                await self._process_message(source, event.message)

        return active

    async def _catch_up(self, active_sources: Iterable[tuple[Source, object]]) -> None:
        buffered: list[tuple[datetime, Source, Message]] = []
        for source, entity in active_sources:
            try:
                async for message in self.user_client.iter_messages(entity, limit=self.config.catch_up_limit):
                    message_date = (message.date.replace(tzinfo=timezone.utc) if message.date else datetime.now(MSK)).astimezone(MSK)
                    buffered.append((message_date, source, message))
            except RPCError as exc:
                LOGGER.warning("Could not catch up %s: %s", source.handle, exc)

        for _, source, message in sorted(buffered, key=lambda item: item[0]):
            await self._process_message(source, message)

    async def _process_message(self, source: Source, message: Message) -> None:
        text = message.message or ""
        if not text.strip():
            return

        # Если это Kwork-канал — новая логика: критерии + генерация + кнопка
        if source.handle == self._kwork_handle:
            # Запускаем в фоне, чтобы не блокировать поток сообщений
            asyncio.create_task(self._handle_kwork_project(source, message, text))
            return

        # Обычные источники: фильтрация как раньше
        match = match_text(text)
        if not match.accepted:
            return

        link = f"https://t.me/{source.username}/{message.id}"
        message_date = _msk_timestamp(message.date)
        lead = LeadRecord(
            source=source.handle,
            message_id=int(message.id),
            link=link,
            text=text,
            score=match.score,
            keywords=match.matched_keywords,
            message_date=message_date,
        )

        # Сохраняем в БД, ставим в очередь
        if not self.storage.record_or_should_retry(lead):
            return  # Уже был отправлен

        await self._queue.put((lead, source))

    async def _wait_until_stopped(self) -> None:
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop_event.set)
            except NotImplementedError:
                pass
        await stop_event.wait()


async def run_app() -> None:
    config = RuntimeConfig.from_env()
    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    app = LeadBot(config)
    try:
        await app.run()
    finally:
        await app.shutdown()


def _human_score(score: int) -> str:
    """Возвращает эмодзи-оценку для score."""
    if score >= 10:
        return "🔥 "
    elif score >= 5:
        return "✅ "
    return ""


def cli() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Monitor Telegram freelance sources and deliver leads.")
    parser.add_argument("--check-filter", help="Check a text against the current keyword filter.")
    args = parser.parse_args()

    if args.check_filter:
        result = match_text(args.check_filter)
        emoji = _human_score(result.score)
        if result.accepted:
            print(f"✅ {emoji}ПРОПУСТИТЬ (score: {result.score})")
            print(f"   Совпало: {', '.join(result.matched_keywords)}")
        else:
            if result.rejected_by:
                print(f"❌ ОТКЛОНЕНО — стоп-слова: {', '.join(result.rejected_by)}")
            else:
                print(f"❌ ОТКЛОНЕНО (score {result.score} < минимального 2)")
                if result.matched_keywords:
                    print(f"   Найдено слов: {', '.join(result.matched_keywords)}")
                else:
                    print(f"   Ни одно ключевое слово не найдено в тексте")
        print()
        print(f"ℹ️  Проверяемый текст: {args.check_filter[:100]}{"…" if len(args.check_filter) > 100 else ""}")
        return

    asyncio.run(run_app())

