from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import time
from datetime import datetime, timezone
from typing import Iterable

from telethon import TelegramClient, events
from telethon.errors import RPCError
from telethon.tl.custom.message import Message

from .config import RuntimeConfig
from .filters import KEYWORDS, STOP_WORDS, match_text
from .formatting import format_lead
from .sources import Source, enabled_sources
from .storage import LeadRecord, Storage


LOGGER = logging.getLogger("freelancer_bot")


class LeadBot:
    def __init__(self, config: RuntimeConfig):
        self.config = config
        config.user_session_path.parent.mkdir(parents=True, exist_ok=True)
        config.bot_session_path.parent.mkdir(parents=True, exist_ok=True)
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
        # Очередь для отправки лидов
        self._queue: asyncio.Queue[tuple[LeadRecord, Source]] = asyncio.Queue()
        self._running = False

    async def run(self) -> None:
        self._register_bot_commands()

        await self.user_client.start()
        await self.bot_client.start(bot_token=self.config.bot_token)

        if self.config.target_chat_id is not None:
            self.storage.add_subscriber(self.config.target_chat_id)

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
        """Отправляет один лид всем подписчикам."""
        subscribers = self.storage.subscribers()
        if not subscribers:
            LOGGER.warning("Lead matched, but no subscribers: %s", lead.link)
            return

        body = format_lead(source, lead)
        delivered = False
        for chat_id in subscribers:
            try:
                await self.bot_client.send_message(chat_id, body, parse_mode="html", link_preview=False)
                delivered = True
            except RPCError as exc:
                LOGGER.warning("Could not deliver lead to %s: %s", chat_id, exc)

        if delivered:
            self.storage.mark_notified(lead.source, lead.message_id)
            LOGGER.info("Delivered lead from %s message %s", source.handle, lead.message_id)

    def _register_bot_commands(self) -> None:
        @self.bot_client.on(events.NewMessage(pattern=r"^/start"))
        async def start(event: events.NewMessage.Event) -> None:
            chat_id = int(event.chat_id)
            self.storage.add_subscriber(chat_id)
            await event.respond(
                "Готово. Этот чат подписан на лиды.\n\n"
                f"Chat ID: <code>{chat_id}</code>\n"
                "Команды: /status, /sources, /keywords, /test текст, /stop",
                parse_mode="html",
            )

        @self.bot_client.on(events.NewMessage(pattern=r"^/stop"))
        async def stop(event: events.NewMessage.Event) -> None:
            self.storage.remove_subscriber(int(event.chat_id))
            await event.respond("Ок, этот чат отписан от уведомлений.")

        @self.bot_client.on(events.NewMessage(pattern=r"^/status"))
        async def status(event: events.NewMessage.Event) -> None:
            stats = self.storage.stats()
            await event.respond(
                "Статус:\n"
                f"- источников: {len(self.sources)}\n"
                f"- подписчиков: {stats['subscribers']}\n"
                f"- лидов в базе: {stats['leads']}\n"
                f"- ожидают повторной отправки: {stats['pending']}"
            )

        @self.bot_client.on(events.NewMessage(pattern=r"^/sources"))
        async def sources(event: events.NewMessage.Event) -> None:
            lines = [f"{index}. {source.handle} — {source.title}" for index, source in enumerate(self.sources, 1)]
            await event.respond("Активные источники:\n" + "\n".join(lines))

        @self.bot_client.on(events.NewMessage(pattern=r"^/keywords"))
        async def keywords(event: events.NewMessage.Event) -> None:
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
                    message_date = message.date or datetime.now(timezone.utc)
                    buffered.append((message_date, source, message))
            except RPCError as exc:
                LOGGER.warning("Could not catch up %s: %s", source.handle, exc)

        for _, source, message in sorted(buffered, key=lambda item: item[0]):
            await self._process_message(source, message)

    async def _process_message(self, source: Source, message: Message) -> None:
        text = message.message or ""
        if not text.strip():
            return

        match = match_text(text)
        if not match.accepted:
            return

        link = f"https://t.me/{source.username}/{message.id}"
        message_date = (message.date or datetime.now(timezone.utc)).isoformat()
        lead = LeadRecord(
            source=source.handle,
            message_id=int(message.id),
            link=link,
            text=text,
            score=match.score,
            keywords=match.matched_keywords,
            message_date=message_date,
        )

        # Сохраняем в БД сразу, но не отправляем — ставим в очередь
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


def cli() -> None:
    parser = argparse.ArgumentParser(description="Monitor Telegram freelance sources and deliver leads.")
    parser.add_argument("--check-filter", help="Check a text against the current keyword filter.")
    args = parser.parse_args()

    if args.check_filter:
        result = match_text(args.check_filter)
        print(result)
        return

    asyncio.run(run_app())
