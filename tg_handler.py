"""Роутеры aiogram и FSM логика Telegram-стороны моста.

Состояния:
  * WaitingForRecipient — выбор собеседника из inline-кнопок;
  * ComposingMessages — накопление сообщений перед пакетной отправкой.

Очередь пакетной отправки хранится в памяти (dict по TG user_id), не в БД.
"""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

import database as db
import media
from vk_listener import VkGateway

logger = logging.getLogger(__name__)

SEND_DELAY = 0.5  # задержка между сообщениями при пакетной отправке, сек

# Подписи кнопок reply-клавиатуры режима составления.
BTN_SEND = "📨 Отправить всё"
BTN_CLEAR = "🗑 Очистить"
BTN_SHOW = "👁 Показать очередь"
BTN_END = "🔚 Завершить диалог"

# Очередь накопленных сообщений: tg_user_id -> list[Message]
_queues: dict[int, list[Message]] = {}


class BridgeStates(StatesGroup):
    WaitingForRecipient = State()
    ComposingMessages = State()


# --- клавиатуры -------------------------------------------------------------

def _recipients_keyboard(users: list[db.VkUser]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"👤 {u.name}", callback_data=f"pick:{u.vk_user_id}")]
        for u in users
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _compose_keyboard(name: str | None = None) -> ReplyKeyboardMarkup:
    """Постоянная reply-клавиатура — висит снизу, не дублируется под сообщениями.

    Имя получателя показываем в подсказке поля ввода, чтобы было видно, кому пишешь.
    """
    placeholder = f"Пишешь → {name}" if name else "Сообщение для отправки в ВК…"
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_SEND)],
            [KeyboardButton(text=BTN_CLEAR), KeyboardButton(text=BTN_SHOW)],
            [KeyboardButton(text=BTN_END)],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder=placeholder,
    )


def _start_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✍️ Выбрать собеседника", callback_data="choose")]
        ]
    )


def build_router(gw: VkGateway, owner_id: int) -> Router:
    """Собрать роутер с привязкой к VK-шлюзу и владельцу бота."""
    router = Router()
    # Реагируем только на владельца — остальных игнорируем.
    router.message.filter(F.from_user.id == owner_id)
    router.edited_message.filter(F.from_user.id == owner_id)
    router.callback_query.filter(F.from_user.id == owner_id)

    # --- команды ----------------------------------------------------------

    @router.message(CommandStart())
    async def cmd_start(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer(
            "🤝 Мост ВКонтакте ↔ Telegram запущен.\n\n"
            "Входящие сообщения из ВК приходят сюда. Чтобы написать первым "
            "или ответить конкретному собеседнику — выбери его из списка, "
            "либо просто сделай reply на нужное сообщение.",
            reply_markup=_start_keyboard(),
        )

    @router.message(Command("cancel"))
    async def cmd_cancel(message: Message, state: FSMContext) -> None:
        _queues.pop(message.from_user.id, None)
        await state.clear()
        await message.answer(
            "❌ Отменено. Вышли из текущего режима.",
            reply_markup=ReplyKeyboardRemove(),
        )

    @router.message(Command("write"))
    async def cmd_write(message: Message, state: FSMContext) -> None:
        await _show_recipients(message, state)

    # --- выбор собеседника ------------------------------------------------

    async def _show_recipients(target: Message, state: FSMContext) -> None:
        users = await db.list_users()
        if not users:
            await target.answer("Пока никто не писал в сообщество — некому отвечать.")
            return
        await state.set_state(BridgeStates.WaitingForRecipient)
        await target.answer("Выбери собеседника:", reply_markup=_recipients_keyboard(users))

    @router.callback_query(F.data == "choose")
    async def cb_choose(callback: CallbackQuery, state: FSMContext) -> None:
        await _show_recipients(callback.message, state)
        await callback.answer()

    @router.callback_query(F.data.startswith("pick:"))
    async def cb_pick(callback: CallbackQuery, state: FSMContext) -> None:
        vk_user_id = int(callback.data.split(":", 1)[1])
        name = await db.get_user_name(vk_user_id) or f"id{vk_user_id}"
        await db.set_last_recipient(vk_user_id)
        await state.set_state(BridgeStates.ComposingMessages)
        await state.update_data(vk_user_id=vk_user_id, vk_user_name=name)
        _queues[callback.from_user.id] = []
        await callback.message.answer(
            f"✏️ Режим составления (получатель: {name}). "
            "Отправляй сообщения по одному — они будут накапливаться.\n"
            "Когда закончишь — нажми «📨 Отправить всё», а чтобы перестать писать "
            "этому собеседнику — «🔚 Завершить диалог».",
            reply_markup=_compose_keyboard(name),
        )
        await callback.answer()

    # --- ответ через reply (приоритетнее накопления) ----------------------

    @router.message(F.reply_to_message)
    async def on_reply(message: Message, state: FSMContext, bot: Bot) -> None:
        replied_id = message.reply_to_message.message_id
        vk_user_id = await db.get_vk_user_by_tg_message(replied_id)
        if vk_user_id is None:
            await message.answer(
                "🤷 Не удалось определить получателя по этому сообщению. "
                "Выбери собеседника через /write."
            )
            return
        # Переотправляем в ВК сообщение, на которое отвечаем (для контекста).
        await _resend_reply_context(message.reply_to_message, vk_user_id)
        await media.send_tg_message_to_vk(bot, gw, vk_user_id, message)
        await db.set_last_recipient(vk_user_id)
        name = await db.get_user_name(vk_user_id) or f"id{vk_user_id}"
        await message.answer(f"✅ Отправлено → {name}")

    async def _resend_reply_context(replied: Message, vk_user_id: int) -> None:
        """Переотправить в ВК сообщение, на которое отвечают.

        Медиа/стикер пересылаем как есть (стикер -> картинка), текст — очищенным.
        """
        if _has_media(replied):
            await media.send_tg_message_to_vk(
                bot, gw, vk_user_id, replied, prefix=f"{media.REPLY_MARK} "
            )
        else:
            ctx = media.clean_reply_text(replied.text or replied.caption or "")
            if ctx:
                await gw.send_message(vk_user_id, message=f"{media.REPLY_MARK} {ctx}")
        await asyncio.sleep(0.3)

    # --- редактирование своего ответа в Telegram -> отправка правки в ВК ---

    @router.edited_message(F.reply_to_message)
    async def on_edit_reply(message: Message, bot: Bot) -> None:
        vk_user_id = await db.get_vk_user_by_tg_message(message.reply_to_message.message_id)
        if vk_user_id is None:
            return
        await _resend_reply_context(message.reply_to_message, vk_user_id)
        await media.send_tg_message_to_vk(
            bot, gw, vk_user_id, message, prefix="✏️ (изменено)\n"
        )
        await db.set_last_recipient(vk_user_id)
        name = await db.get_user_name(vk_user_id) or f"id{vk_user_id}"
        await message.answer(f"✏️ Изменение отправлено → {name}")

    # --- кнопки reply-клавиатуры (обрабатываем раньше накопления) ----------

    @router.message(BridgeStates.ComposingMessages, F.text == BTN_SEND)
    async def btn_send(message: Message, state: FSMContext, bot: Bot) -> None:
        queue = _queues.get(message.from_user.id, [])
        if not queue:
            await message.answer("Очередь пуста — нечего отправлять.")
            return
        data = await state.get_data()
        vk_user_id = data.get("vk_user_id")
        name = data.get("vk_user_name", f"id{vk_user_id}")
        await message.answer("📤 Отправляю…")

        count = 0
        for m in queue:
            await media.send_tg_message_to_vk(bot, gw, vk_user_id, m)
            count += 1
            await asyncio.sleep(SEND_DELAY)

        _queues[message.from_user.id] = []
        await message.answer(f"✅ Отправлено {count} {_plural(count)} → {name}")

    @router.message(BridgeStates.ComposingMessages, F.text == BTN_CLEAR)
    async def btn_clear(message: Message, state: FSMContext) -> None:
        _queues[message.from_user.id] = []
        await message.answer("🗑 Очередь очищена.")

    @router.message(BridgeStates.ComposingMessages, F.text == BTN_SHOW)
    async def btn_show(message: Message, state: FSMContext) -> None:
        queue = _queues.get(message.from_user.id, [])
        if not queue:
            await message.answer("Очередь пуста.")
            return
        lines = [f"{i}. {_describe(m)}" for i, m in enumerate(queue, 1)]
        await message.answer("👁 В очереди:\n" + "\n".join(lines))

    @router.message(BridgeStates.ComposingMessages, F.text == BTN_END)
    async def btn_end(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        name = data.get("vk_user_name", "собеседником")
        _queues.pop(message.from_user.id, None)
        await state.clear()
        await message.answer(
            f"🔚 Диалог с {name} завершён. Сообщения больше никому не уходят — "
            "выбери собеседника через /write, когда понадобится.",
            reply_markup=ReplyKeyboardRemove(),
        )

    # --- накопление сообщений ---------------------------------------------

    @router.message(BridgeStates.ComposingMessages)
    async def on_compose(message: Message, state: FSMContext) -> None:
        queue = _queues.setdefault(message.from_user.id, [])
        queue.append(message)
        # Клавиатура уже висит снизу — под каждым сообщением её не повторяем.
        await message.answer(f"➕ Добавлено ({len(queue)} в очереди)")

    # --- подсказка вне состояний ------------------------------------------

    @router.message()
    async def fallback(message: Message, state: FSMContext) -> None:
        await message.answer(
            "Чтобы написать в ВК — выбери собеседника через /write "
            "или ответь reply на нужное сообщение."
        )

    return router


# --- вспомогательное --------------------------------------------------------

def _has_media(message: Message) -> bool:
    """Есть ли в сообщении медиа/стикер (а не только текст)."""
    return any(
        (
            message.sticker,
            message.photo,
            message.video,
            message.voice,
            message.animation,
            message.document,
            message.audio,
            message.video_note,
        )
    )


def _describe(message: Message) -> str:
    if message.text:
        text = message.text
        return text if len(text) <= 50 else text[:50] + "…"
    if message.photo:
        return "🖼 фото"
    if message.video:
        return "🎬 видео"
    if message.voice:
        return "🎤 голосовое"
    if message.animation:
        return "🎞 GIF"
    if message.sticker:
        return "🩷 стикер"
    if message.audio:
        return "🎵 аудио"
    if message.document:
        return f"📄 {message.document.file_name or 'документ'}"
    return "вложение"


def _plural(n: int) -> str:
    """Склонение слова «сообщение» для русского текста."""
    if 11 <= n % 100 <= 14:
        return "сообщений"
    last = n % 10
    if last == 1:
        return "сообщение"
    if 2 <= last <= 4:
        return "сообщения"
    return "сообщений"
