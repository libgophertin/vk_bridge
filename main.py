"""Точка входа: параллельный запуск VK Longpoll и Telegram polling."""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
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


def _make_incoming_handler(bot: Bot, gw: VkGateway, owner_id: int):
    """Создать обработчик входящих VK-сообщений, замкнутый на bot/gw."""

    async def handle(message: dict) -> None:
        from_id = message.get("from_id")
        # Обрабатываем только сообщения от пользователей (id > 0).
        if not from_id or from_id <= 0:
            return

        # Резолвим имя: сперва из БД, при отсутствии — через VK API.
        name = await db.get_user_name(from_id)
        if name is None:
            name = await gw.fetch_user_name(from_id)
        await db.upsert_user(from_id, name)

        header = f"👤 {name} (vk.com/id{from_id}): "
        sent_ids = await media.forward_to_telegram(bot, owner_id, header, message)

        # Связываем все отправленные TG-сообщения с собеседником (для reply).
        for mid in sent_ids:
            await db.save_message_link(mid, from_id)
        await db.set_last_recipient(from_id)
        logger.info("Переслано сообщение от %s (%s) в Telegram", name, from_id)

    return handle


async def main() -> None:
    db.configure(settings.db_path)
    await db.init_db()

    bot = Bot(token=settings.tg_bot_token)
    dp = Dispatcher(storage=MemoryStorage())

    gw = VkGateway(settings.vk_token)
    await gw.setup()

    dp.include_router(build_router(gw, settings.tg_owner_id))

    incoming = _make_incoming_handler(bot, gw, settings.tg_owner_id)

    logger.info("Запуск моста ВКонтакте ↔ Telegram")
    await asyncio.gather(
        gw.run(incoming),
        dp.start_polling(bot),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Остановка по сигналу")
