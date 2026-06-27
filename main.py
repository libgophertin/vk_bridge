"""Точка входа: параллельный запуск VK Longpoll и Telegram polling."""

from __future__ import annotations

import asyncio
import logging
import time

from aiogram import Bot, Dispatcher, html
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatAction, ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

import database as db
import media
from config import settings
from tg_handler import build_router
from vk_listener import VkGateway

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("vk_bridge")


class ReadWatcher:
    """Отслеживает прочтение исходящих сообщений и уведомляет владельца.

    В ВК нет события «прочитано», поэтому периодически опрашиваем out_read
    (id последнего прочитанного собеседником исходящего сообщения).
    """

    POLL_INTERVAL = 4      # как часто опрашивать ВК, сек
    TIMEOUT = 15 * 60      # сколько ждать прочтения, прежде чем бросить, сек

    def __init__(self, gw: VkGateway, bot: Bot, owner_id: int) -> None:
        self._gw = gw
        self._bot = bot
        self._owner_id = owner_id
        # peer_id -> {"msg_id", "name", "ts"}
        self._pending: dict[int, dict] = {}

    def track(self, peer_id: int, msg_id: int, name: str) -> None:
        """Начать ждать прочтения сообщения msg_id собеседником peer_id."""
        self._pending[peer_id] = {"msg_id": msg_id, "name": name, "ts": time.monotonic()}

    async def run(self) -> None:
        while True:
            await asyncio.sleep(self.POLL_INTERVAL)
            for peer_id, info in list(self._pending.items()):
                if time.monotonic() - info["ts"] > self.TIMEOUT:
                    self._pending.pop(peer_id, None)
                    continue
                try:
                    out_read = await self._gw.get_out_read(peer_id)
                except Exception:  # noqa: BLE001
                    logger.debug("Не удалось проверить прочтение для %s", peer_id)
                    continue
                if out_read and out_read >= info["msg_id"]:
                    self._pending.pop(peer_id, None)
                    try:
                        await self._bot.send_message(
                            self._owner_id, f"👁 Прочитано → {html.quote(info['name'])}"
                        )
                    except Exception:  # noqa: BLE001
                        logger.debug("Не удалось отправить статус «прочитано»")


def _make_incoming_handler(bot: Bot, gw: VkGateway, owner_id: int):
    """Создать обработчик входящих VK-сообщений, замкнутый на bot/gw."""

    async def handle(message: dict, is_edit: bool = False) -> None:
        from_id = message.get("from_id")
        # Обрабатываем только сообщения от пользователей (id > 0).
        if not from_id or from_id <= 0:
            return

        # Резолвим имя: сперва из БД, при отсутствии — через VK API.
        name = await db.get_user_name(from_id)
        if name is None:
            name = await gw.fetch_user_name(from_id)
        await db.upsert_user(from_id, name)

        # Контекст ответа: текст встраиваем в то же сообщение (меньше уведомлений),
        # а медиа/стикер нельзя слить — отправляем отдельным сообщением.
        reply_context = ""
        reply = message.get("reply_message")
        if reply and (reply.get("text") or reply.get("attachments")):
            if reply.get("attachments"):
                reply_clean = dict(reply)
                reply_clean["text"] = media.clean_reply_text(reply.get("text") or "")
                try:
                    await media.forward_to_telegram(
                        bot, owner_id, f"{media.REPLY_MARK} ", reply_clean
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("Не удалось переотправить сообщение-контекст")
            else:
                ctx = media.clean_reply_text(reply.get("text") or "")
                if ctx:
                    reply_context = f"{media.REPLY_MARK} {html.quote(ctx)}\n\n"

        # Красивое имя: кликабельная жирная ссылка на профиль.
        link = html.link(html.bold(html.quote(name)), f"https://vk.com/id{from_id}")
        verb = " изменил(а)" if is_edit else ""
        mark = "✏️" if is_edit else "👤"
        header = f"{mark} {link}{verb}: "
        sent_ids = await media.forward_to_telegram(
            bot, owner_id, header, message, reply_context=reply_context
        )

        # Связываем все отправленные TG-сообщения с собеседником (для reply).
        for mid in sent_ids:
            await db.save_message_link(mid, from_id)
        await db.set_last_recipient(from_id)
        # Отмечаем входящее прочитанным — собеседник в ВК видит галочку.
        await gw.mark_as_read(from_id)
        logger.info("Переслано сообщение от %s (%s) в Telegram", name, from_id)

    return handle


async def main() -> None:
    db.configure(settings.db_path)
    await db.init_db()

    bot = Bot(
        token=settings.tg_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())

    gw = VkGateway(settings.vk_token)
    await gw.setup()

    read_watcher = ReadWatcher(gw, bot, settings.tg_owner_id)
    dp.include_router(build_router(gw, settings.tg_owner_id, on_sent=read_watcher.track))

    incoming = _make_incoming_handler(bot, gw, settings.tg_owner_id)

    async def on_typing(vk_user_id: int) -> None:
        """Кто-то печатает в ВК -> показываем «печатает…» в Telegram."""
        try:
            await bot.send_chat_action(settings.tg_owner_id, action=ChatAction.TYPING)
        except Exception:  # noqa: BLE001
            logger.debug("Не удалось отправить статус «печатает» в Telegram")

    logger.info("Запуск моста ВКонтакте ↔ Telegram")
    await asyncio.gather(
        gw.run(incoming, on_typing),
        read_watcher.run(),
        dp.start_polling(bot),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Остановка по сигналу")
