"""Слой работы с ВКонтакте.

VkGateway инкапсулирует синхронный vk_api:
  * слушает Longpoll сообщества и отдаёт события в asyncio-цикл;
  * резолвит имена пользователей;
  * отправляет текст и загружает медиа (фото/документы/голос) в личку.

Так как vk_api синхронный, блокирующие вызовы оборачиваются в asyncio.to_thread,
а сам цикл Longpoll крутится в отдельном потоке через run_in_executor.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable

import vk_api
from vk_api.bot_longpoll import VkBotEventType, VkBotLongPoll
from vk_api.exceptions import ApiError

logger = logging.getLogger(__name__)

EventHandler = Callable[[dict], Awaitable[None]]


def _random_id() -> int:
    return random.randint(1, 2**31 - 1)


def _attachment_str(prefix: str, owner_id: int, media_id: int, access_key: str | None) -> str:
    base = f"{prefix}{owner_id}_{media_id}"
    return f"{base}_{access_key}" if access_key else base


class VkGateway:
    def __init__(self, token: str) -> None:
        self._token = token
        self._session: vk_api.VkApi | None = None
        self._api = None
        self._upload: vk_api.VkUpload | None = None
        self.group_id: int | None = None

    # --- инициализация (синхронная, вызывается через to_thread) ---------------

    def _setup_sync(self) -> None:
        self._session = vk_api.VkApi(token=self._token)
        self._api = self._session.get_api()
        self._upload = vk_api.VkUpload(self._session)
        group = self._api.groups.getById()
        # В новых версиях API ответ — {'groups': [...]}, в старых — просто список.
        groups = group["groups"] if isinstance(group, dict) else group
        self.group_id = int(groups[0]["id"])
        logger.info("Подключено к сообществу VK, group_id=%s", self.group_id)

    async def setup(self) -> None:
        await asyncio.to_thread(self._setup_sync)

    # --- имена пользователей --------------------------------------------------

    def _fetch_name_sync(self, vk_user_id: int) -> str:
        try:
            users = self._api.users.get(user_ids=vk_user_id)
            if users:
                u = users[0]
                return f"{u.get('first_name', '')} {u.get('last_name', '')}".strip()
        except Exception:  # noqa: BLE001
            logger.exception("Не удалось получить имя пользователя %s", vk_user_id)
        return f"id{vk_user_id}"

    async def fetch_user_name(self, vk_user_id: int) -> str:
        return await asyncio.to_thread(self._fetch_name_sync, vk_user_id)

    # --- отправка -------------------------------------------------------------

    def _send_sync(self, user_id: int, message: str = "", attachment: str = "") -> None:
        self._api.messages.send(
            user_id=user_id,
            random_id=_random_id(),
            message=message or "",
            attachment=attachment or "",
        )

    async def send_message(self, user_id: int, message: str = "", attachment: str = "") -> None:
        await asyncio.to_thread(self._send_sync, user_id, message, attachment)

    def _upload_photo_sync(self, path: str) -> str:
        photos = self._upload.photo_messages(path)
        p = photos[0]
        return _attachment_str("photo", p["owner_id"], p["id"], p.get("access_key"))

    async def upload_photo(self, path: str) -> str:
        return await asyncio.to_thread(self._upload_photo_sync, path)

    def _upload_doc_sync(self, user_id: int, path: str, title: str) -> str:
        result = self._upload.document_message(path, peer_id=user_id, title=title)
        d = result["doc"]
        return _attachment_str("doc", d["owner_id"], d["id"], d.get("access_key"))

    async def upload_document(self, user_id: int, path: str, title: str = "file") -> str:
        return await asyncio.to_thread(self._upload_doc_sync, user_id, path, title)

    def _upload_voice_sync(self, user_id: int, path: str) -> str:
        result = self._upload.audio_message(path, peer_id=user_id)
        a = result["audio_message"]
        return _attachment_str("doc", a["owner_id"], a["id"], a.get("access_key"))

    async def upload_voice(self, user_id: int, path: str) -> str:
        return await asyncio.to_thread(self._upload_voice_sync, user_id, path)

    # --- Longpoll -------------------------------------------------------------

    def _listen_sync(self, on_event: EventHandler, loop: asyncio.AbstractEventLoop) -> None:
        while True:
            try:
                # Создание longpoll тоже внутри цикла: если Bots Long Poll ещё не
                # включён или была сетевая ошибка — пробуем снова, а не падаем.
                longpoll = VkBotLongPoll(self._session, group_id=self.group_id)
                logger.info("Запущен VK Longpoll")
                for event in longpoll.listen():
                    if event.type != VkBotEventType.MESSAGE_NEW:
                        continue
                    obj = event.object
                    # API 5.103+: объект содержит ключ "message"; раньше — сам объект.
                    message = obj.get("message", obj) if hasattr(obj, "get") else obj
                    future = asyncio.run_coroutine_threadsafe(on_event(dict(message)), loop)
                    try:
                        future.result()  # пробрасываем исключения в лог
                    except Exception:  # noqa: BLE001
                        logger.exception("Ошибка обработки входящего VK-сообщения")
            except ApiError as exc:
                if getattr(exc, "code", None) == 15:
                    # [15] — у токена нет прав на groups.getLongPollServer.
                    logger.error(
                        "VK API [15] Access denied: у токена сообщества нет нужных прав.\n"
                        "Пересоздай ключ доступа с правом «Управление сообществом» "
                        "(Управление → Работа с API → Ключи доступа → Создать ключ) — "
                        "именно оно даёт доступ к Long Poll. Также проверь, что Long Poll "
                        "API включён. Повтор через 30 сек.",
                    )
                    time.sleep(30)
                else:
                    logger.error(
                        "Ошибка VK API при запуске Longpoll: %s\n"
                        "Проверь: Управление → Работа с API → Long Poll API → Включено, "
                        "последняя версия, тип события «Входящее сообщение». "
                        "Повтор через 15 сек.",
                        exc,
                    )
                    time.sleep(15)
            except Exception:  # noqa: BLE001
                # Сетевые сбои Longpoll не должны ронять бота — переподключаемся.
                logger.exception("Сбой VK Longpoll, переподключение через 5 сек")
                time.sleep(5)

    async def run(self, on_event: EventHandler) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._listen_sync, on_event, loop)
