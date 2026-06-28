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

# Обработчик получает сам объект сообщения и флаг «это редактирование».
EventHandler = Callable[[dict, bool], Awaitable[None]]
# Обработчик статуса «печатает»: получает vk_user_id печатающего.
TypingHandler = Callable[[int], Awaitable[None]]


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

    def _send_sync(
        self, user_id: int, message: str = "", attachment: str = "", reply_to: int | None = None
    ) -> int:
        params = {
            "user_id": user_id,
            "random_id": _random_id(),
            "message": message or "",
            "attachment": attachment or "",
        }
        if reply_to:
            params["reply_to"] = reply_to
        return self._api.messages.send(**params)

    async def send_message(
        self,
        user_id: int,
        message: str = "",
        attachment: str = "",
        reply_to: int | None = None,
    ) -> int | None:
        """Отправить сообщение, вернуть его id.

        reply_to — id сообщения в ВК, на которое отвечаем (нативный ответ).
        """
        return await asyncio.to_thread(
            self._send_sync, user_id, message, attachment, reply_to
        )

    # --- статусы прочтения ----------------------------------------------------

    def _mark_read_sync(self, user_id: int) -> None:
        self._api.messages.markAsRead(peer_id=user_id, group_id=self.group_id)

    async def mark_as_read(self, user_id: int) -> None:
        """Отметить переписку с пользователем как прочитанную сообществом."""
        try:
            await asyncio.to_thread(self._mark_read_sync, user_id)
        except Exception:  # noqa: BLE001
            logger.debug("Не удалось отметить прочитанным диалог с %s", user_id)

    def _get_out_read_sync(self, user_id: int) -> int:
        res = self._api.messages.getConversationsById(
            peer_ids=user_id, group_id=self.group_id
        )
        items = res.get("items") or []
        if items:
            return int(items[0].get("out_read") or 0)
        return 0

    async def get_out_read(self, user_id: int) -> int:
        """Id последнего исходящего сообщения, прочитанного собеседником."""
        return await asyncio.to_thread(self._get_out_read_sync, user_id)

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

    # --- статус «печатает» ----------------------------------------------------

    def _set_typing_sync(self, user_id: int) -> None:
        try:
            self._api.messages.setActivity(
                user_id=user_id, type="typing", group_id=self.group_id
            )
        except Exception:  # noqa: BLE001
            logger.debug("Не удалось выставить статус «печатает» для %s", user_id)

    async def set_typing(self, user_id: int) -> None:
        await asyncio.to_thread(self._set_typing_sync, user_id)

    # --- Longpoll -------------------------------------------------------------

    def _listen_sync(
        self,
        on_event: EventHandler,
        on_typing: TypingHandler | None,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        while True:
            try:
                # Создание longpoll тоже внутри цикла: если Bots Long Poll ещё не
                # включён или была сетевая ошибка — пробуем снова, а не падаем.
                longpoll = VkBotLongPoll(self._session, group_id=self.group_id)
                logger.info("Запущен VK Longpoll")
                for event in longpoll.listen():
                    # Статус «печатает» из ВК -> прокидываем в Telegram.
                    if event.type == VkBotEventType.MESSAGE_TYPING_STATE:
                        if on_typing is None:
                            continue
                        typer = event.object.get("from_id")
                        if typer and typer > 0:
                            asyncio.run_coroutine_threadsafe(on_typing(typer), loop)
                        continue

                    if event.type not in (
                        VkBotEventType.MESSAGE_NEW,
                        VkBotEventType.MESSAGE_EDIT,
                    ):
                        continue
                    is_edit = event.type == VkBotEventType.MESSAGE_EDIT
                    obj = event.object
                    # MESSAGE_NEW (API 5.103+): объект содержит ключ "message".
                    # MESSAGE_EDIT: объект сам и есть сообщение.
                    message = obj.get("message", obj) if hasattr(obj, "get") else obj
                    future = asyncio.run_coroutine_threadsafe(
                        on_event(dict(message), is_edit), loop
                    )
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

    async def run(
        self, on_event: EventHandler, on_typing: TypingHandler | None = None
    ) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._listen_sync, on_event, on_typing, loop)
