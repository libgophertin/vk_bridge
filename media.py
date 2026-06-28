"""Проброс медиа в обе стороны и конвертация форматов.

VK -> TG: рендер входящего сообщения сообщества с вложениями в Telegram.
TG -> VK: загрузка файлов из Telegram, конвертация и отправка в ВК.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile

from aiogram import Bot, html
from aiogram.types import Message, ReplyParameters, URLInputFile
from PIL import Image

from vk_listener import VkGateway

logger = logging.getLogger(__name__)

# Экранирование пользовательского текста для HTML-разметки Telegram.
_esc = html.quote

# Упоминания ВК: [id123|Имя], [club123|Имя], [public123|Имя].
_MENTION_RE = re.compile(r"\[(id|club|public)(\d+)\|([^\]]+)\]")


def render_vk_text(text: str) -> str:
    """Подготовить текст из ВК к HTML Telegram.

    Упоминания [id123|Имя] превращает в кликабельные ссылки, остальной текст
    экранирует. Возвращает HTML-безопасную строку.
    """
    text = text or ""
    out: list[str] = []
    pos = 0
    for m in _MENTION_RE.finditer(text):
        out.append(_esc(text[pos:m.start()]))
        kind, num, name = m.group(1), m.group(2), m.group(3)
        out.append(html.link(_esc(name), f"https://vk.com/{kind}{num}"))
        pos = m.end()
    out.append(_esc(text[pos:]))
    return "".join(out)


def _vk_body(message: dict) -> str:
    """Собрать HTML-тело входящего: текст + пересланные сообщения (fwd)."""
    body = render_vk_text(message.get("text", "") or "")
    fwd = message.get("fwd_messages") or []
    fwd_lines = []
    for fm in fwd:
        ft = render_vk_text((fm.get("text") or "").strip())
        if not ft and fm.get("attachments"):
            ft = "[вложение]"
        if ft:
            fwd_lines.append(f"⏩ {ft}")
    if fwd_lines:
        block = "\n".join(fwd_lines)
        body = f"{body}\n{block}" if body else block
    return body

# Telegram Bot API позволяет боту скачивать файлы примерно до 20 МБ.
TG_DOWNLOAD_LIMIT = 20 * 1024 * 1024

# Пометка переотправленного (процитированного) сообщения.
REPLY_MARK = "↩️"

# Максимальная длина переотправляемого текста, чтобы не раздувать сообщение.
CONTEXT_MAX_LEN = 400


# ---------------------------------------------------------------------------
# Контекст ответа (общее для обоих направлений)
#
# Вместо кавычек сообщение, на которое отвечают, переотправляется отдельным
# сообщением с пометкой ↩️. При этом убираются:
#   * шапка отправителя «👤 Имя (vk.com/id1): »;
#   * сама пометка ↩️ от прошлой переотправки;
#   * вложенная цитата «...» от старых сообщений,
# чтобы не плодить вложенные ответы.
# ---------------------------------------------------------------------------

def _strip_sender_header(text: str) -> str:
    """Убрать префикс отправителя вида «👤 Имя: » или «👤 Имя (vk.com/id1): »."""
    if text.startswith(("👤", "✏️")):
        marker = ": "
        idx = text.find(marker)
        if idx != -1:
            return text[idx + len(marker):].strip()
    return text


def clean_reply_text(raw: str) -> str:
    """Достать чистый текст сообщения для переотправки.

    Убирает декорации, которые бот добавляет к пересланным сообщениям:
      * шапку отправителя «👤 Имя: » (как инлайном, так и отдельной строкой);
      * строки контекста ↩️ ... (чтобы не плодить вложенные ответы);
      * старую цитату «...» в начале.
    """
    text = (raw or "").strip()

    # Старый формат с кавычками «...» в начале.
    if text.startswith("«"):
        end = text.find("»")
        if end != -1:
            text = text[end + 1:].strip()

    lines = text.split("\n")

    # Первая строка может быть шапкой отправителя.
    if lines and lines[0].startswith(("👤", "✏️")):
        inline = _strip_sender_header(lines[0])
        if inline and inline != lines[0]:
            lines[0] = inline       # «👤 Имя: текст» -> оставляем текст
        else:
            lines.pop(0)            # «👤 Имя:» отдельной строкой -> убираем строку

    # Снимаем ведущие строки контекста ↩️ и пустые строки.
    while lines and (lines[0].startswith(REPLY_MARK) or not lines[0].strip()):
        lines.pop(0)

    text = "\n".join(lines).strip()
    if len(text) > CONTEXT_MAX_LEN:
        text = text[:CONTEXT_MAX_LEN].rstrip() + "…"
    return text


# ---------------------------------------------------------------------------
# VK -> Telegram
# ---------------------------------------------------------------------------

def _largest_image_url(images: list[dict]) -> str | None:
    """Выбрать URL картинки максимального размера (для фото/стикеров)."""
    if not images:
        return None
    best = max(images, key=lambda s: s.get("width", 0) * s.get("height", 0) or s.get("width", 0))
    return best.get("url") or best.get("src")


async def forward_to_telegram(
    bot: Bot,
    chat_id: int,
    header: str,
    message: dict,
    reply_context: str = "",
    reply_to_message_id: int | None = None,
) -> list[int]:
    """Отправить входящее VK-сообщение владельцу в Telegram.

    `header` и `reply_context` уже HTML-безопасны; пользовательский текст
    экранируется здесь. `reply_to_message_id` — нативный ответ Telegram на ранее
    пересланное сообщение. Возвращает id всех отправленных сообщений.
    """
    attachments = message.get("attachments", []) or []
    body = _vk_body(message)  # текст + упоминания + пересланные сообщения
    # Порядок как в Telegram: сперва отправитель, под ним — на что отвечает, затем текст.
    if reply_context:
        caption = f"{header.rstrip()}\n{reply_context}{body}".rstrip()
    else:
        caption = f"{header}{body}".rstrip()
    sent_ids: list[int] = []
    caption_used = False
    reply_used = False

    async def _cap() -> str:
        """Подпись используем только один раз — на первом вложении."""
        nonlocal caption_used
        if not caption_used:
            caption_used = True
            return caption
        return ""

    def _rp() -> dict:
        """reply_parameters добавляем только к первому сообщению."""
        nonlocal reply_used
        if reply_to_message_id and not reply_used:
            reply_used = True
            return {
                "reply_parameters": ReplyParameters(
                    message_id=reply_to_message_id, allow_sending_without_reply=True
                )
            }
        return {}

    # Нет вложений — обычное текстовое сообщение.
    if not attachments:
        msg = await bot.send_message(chat_id, caption or header, **_rp())
        return [msg.message_id]

    for att in attachments:
        a_type = att.get("type")
        try:
            if a_type == "photo":
                url = _largest_image_url(att["photo"].get("sizes", []))
                if url:
                    m = await bot.send_photo(
                        chat_id, URLInputFile(url), caption=await _cap(), **_rp()
                    )
                    sent_ids.append(m.message_id)

            elif a_type == "sticker":
                sticker = att["sticker"]
                images = sticker.get("images_with_background") or sticker.get("images") or []
                url = _largest_image_url(images)
                if url:
                    m = await bot.send_photo(
                        chat_id, URLInputFile(url), caption=await _cap(), **_rp()
                    )
                    sent_ids.append(m.message_id)

            elif a_type == "audio_message":
                am = att["audio_message"]
                url = am.get("link_ogg") or am.get("link_mp3")
                if url:
                    m = await bot.send_voice(
                        chat_id, URLInputFile(url), caption=await _cap(), **_rp()
                    )
                    sent_ids.append(m.message_id)

            elif a_type == "doc":
                doc = att["doc"]
                url = doc.get("url")
                if not url:
                    continue
                if (doc.get("ext") or "").lower() == "gif":
                    m = await bot.send_animation(
                        chat_id, URLInputFile(url), caption=await _cap(), **_rp()
                    )
                else:
                    m = await bot.send_document(
                        chat_id,
                        URLInputFile(url, filename=doc.get("title", "file")),
                        caption=await _cap(),
                        **_rp(),
                    )
                sent_ids.append(m.message_id)

            elif a_type == "video":
                video = att["video"]
                link = f"https://vk.com/video{video['owner_id']}_{video['id']}"
                title = _esc(video.get("title", "видео"))
                body = f"{await _cap()}\n🎬 {title}: {link}".strip()
                m = await bot.send_message(chat_id, body, **_rp())
                sent_ids.append(m.message_id)

            elif a_type == "audio":
                audio = att["audio"]
                url = audio.get("url")
                raw_title = f"{audio.get('artist', '')} — {audio.get('title', '')}".strip(" —")
                if url:
                    # title в send_audio — это метаданные плеера, не HTML.
                    m = await bot.send_audio(
                        chat_id, URLInputFile(url), caption=await _cap(),
                        title=raw_title[:64], **_rp(),
                    )
                else:
                    m = await bot.send_message(
                        chat_id, f"{await _cap()}\n🎵 {_esc(raw_title)}".strip(), **_rp()
                    )
                sent_ids.append(m.message_id)

            else:
                # wall, link, market и прочее — отдаём текстом.
                m = await bot.send_message(
                    chat_id, f"{await _cap()}\n📎 Вложение типа «{_esc(a_type)}»".strip(), **_rp()
                )
                sent_ids.append(m.message_id)

        except Exception:  # noqa: BLE001
            logger.exception("Не удалось переслать вложение типа %s", a_type)

    # Если ни одно вложение не отправилось, но был текст/заголовок — отправим его.
    if not sent_ids:
        m = await bot.send_message(chat_id, caption or header, **_rp())
        sent_ids.append(m.message_id)

    return sent_ids


# ---------------------------------------------------------------------------
# Telegram -> VK
# ---------------------------------------------------------------------------

def _webp_to_png(src_path: str) -> str:
    """Конвертировать .webp стикер в .png, вернуть путь к png."""
    png_path = src_path.rsplit(".", 1)[0] + ".png"
    with Image.open(src_path) as im:
        im.convert("RGBA").save(png_path, "PNG")
    return png_path


async def _download_to_temp(bot: Bot, file_id: str, suffix: str) -> str:
    """Скачать файл из Telegram во временный файл, вернуть путь."""
    fd, path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    await bot.download(file_id, destination=path)
    return path


def _cleanup(*paths: str) -> None:
    for p in paths:
        try:
            if p and os.path.exists(p):
                os.remove(p)
        except OSError:
            logger.warning("Не удалось удалить временный файл %s", p)


async def send_tg_message_to_vk(
    bot: Bot,
    gw: VkGateway,
    user_id: int,
    message: Message,
    prefix: str = "",
    reply_to: int | None = None,
) -> int | None:
    """Перевести одно сообщение Telegram в ВК нужному собеседнику.

    Поддерживает текст, фото, видео, голос, документы, GIF и стикеры.
    `prefix` — необязательная приписка в начало (например, пометка о правке).
    `reply_to` — id сообщения ВК, на которое отвечаем (нативный ответ).
    Возвращает id отправленного VK-сообщения (для отслеживания прочтения) или None.
    Ошибки на одном сообщении не должны ронять остальную отправку.
    """
    lead = prefix
    caption = lead + (message.caption or "")
    tmp_paths: list[str] = []
    try:
        # --- чистый текст ---
        if message.text:
            return await gw.send_message(
                user_id, message=lead + message.text, reply_to=reply_to
            )

        # --- фото ---
        if message.photo:
            path = await _download_to_temp(bot, message.photo[-1].file_id, ".jpg")
            tmp_paths.append(path)
            attachment = await gw.upload_photo(path)
            return await gw.send_message(
                user_id, message=caption, attachment=attachment, reply_to=reply_to
            )

        # --- голосовое ---
        if message.voice:
            path = await _download_to_temp(bot, message.voice.file_id, ".ogg")
            tmp_paths.append(path)
            attachment = await gw.upload_voice(user_id, path)
            return await gw.send_message(
                user_id, message=caption, attachment=attachment, reply_to=reply_to
            )

        # --- видео ---
        if message.video:
            if message.video.file_size and message.video.file_size > TG_DOWNLOAD_LIMIT:
                return await gw.send_message(
                    user_id,
                    message=(caption + "\n🎬 Видео слишком большое для пересылки.").strip(),
                    reply_to=reply_to,
                )
            path = await _download_to_temp(bot, message.video.file_id, ".mp4")
            tmp_paths.append(path)
            attachment = await gw.upload_document(user_id, path, title="video.mp4")
            return await gw.send_message(
                user_id, message=caption, attachment=attachment, reply_to=reply_to
            )

        # --- GIF / анимация ---
        if message.animation:
            path = await _download_to_temp(bot, message.animation.file_id, ".mp4")
            tmp_paths.append(path)
            attachment = await gw.upload_document(user_id, path, title="animation.mp4")
            return await gw.send_message(
                user_id, message=caption, attachment=attachment, reply_to=reply_to
            )

        # --- стикер Telegram -> картинка в ВК ---
        if message.sticker:
            if message.sticker.is_animated or message.sticker.is_video:
                return await gw.send_message(
                    user_id, message=lead + (message.sticker.emoji or "🙂"), reply_to=reply_to
                )
            webp = await _download_to_temp(bot, message.sticker.file_id, ".webp")
            tmp_paths.append(webp)
            png = await asyncio.to_thread(_webp_to_png, webp)
            tmp_paths.append(png)
            attachment = await gw.upload_photo(png)
            return await gw.send_message(
                user_id, message=lead, attachment=attachment, reply_to=reply_to
            )

        # --- аудиофайл ---
        if message.audio:
            path = await _download_to_temp(bot, message.audio.file_id, ".mp3")
            tmp_paths.append(path)
            title = message.audio.file_name or "audio.mp3"
            attachment = await gw.upload_document(user_id, path, title=title)
            return await gw.send_message(
                user_id, message=caption, attachment=attachment, reply_to=reply_to
            )

        # --- документ / файл ---
        if message.document:
            suffix = os.path.splitext(message.document.file_name or "")[1] or ".bin"
            path = await _download_to_temp(bot, message.document.file_id, suffix)
            tmp_paths.append(path)
            title = message.document.file_name or "file"
            attachment = await gw.upload_document(user_id, path, title=title)
            return await gw.send_message(
                user_id, message=caption, attachment=attachment, reply_to=reply_to
            )

        # --- ничего из перечисленного ---
        logger.warning("Тип сообщения Telegram не поддержан для отправки в ВК")

    except Exception:  # noqa: BLE001
        logger.exception("Не удалось отправить сообщение в ВК пользователю %s", user_id)
    finally:
        _cleanup(*tmp_paths)
