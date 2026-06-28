"""Асинхронная работа с SQLite через aiosqlite.

Хранит:
  * соответствие vk_user_id <-> имя пользователя (таблица users);
  * последнего активного собеседника (таблица app_state);
  * связь tg_message_id -> vk_user_id для корректного reply (таблица msg_links).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import aiosqlite

logger = logging.getLogger(__name__)

_db_path = "bridge.db"


@dataclass
class VkUser:
    vk_user_id: int
    name: str
    last_active: float


def configure(db_path: str) -> None:
    """Задать путь к файлу БД (вызывается из main до init_db)."""
    global _db_path
    _db_path = db_path


async def init_db() -> None:
    """Создать таблицы, если их ещё нет."""
    async with aiosqlite.connect(_db_path) as db:
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                vk_user_id  INTEGER PRIMARY KEY,
                name        TEXT NOT NULL,
                last_active REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS app_state (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS msg_links (
                tg_message_id INTEGER PRIMARY KEY,
                vk_user_id    INTEGER NOT NULL,
                vk_message_id INTEGER,
                created_at    REAL NOT NULL
            );
            """
        )
        # Миграция для старых баз: добавить колонку vk_message_id, если её нет.
        async with db.execute("PRAGMA table_info(msg_links)") as cur:
            cols = [row[1] for row in await cur.fetchall()]
        if "vk_message_id" not in cols:
            await db.execute("ALTER TABLE msg_links ADD COLUMN vk_message_id INTEGER")
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_msg_links_vk ON msg_links(vk_message_id)"
        )
        await db.commit()
    logger.info("База данных инициализирована: %s", _db_path)


async def upsert_user(vk_user_id: int, name: str) -> None:
    """Добавить/обновить пользователя и отметить время активности."""
    async with aiosqlite.connect(_db_path) as db:
        await db.execute(
            """
            INSERT INTO users (vk_user_id, name, last_active)
            VALUES (?, ?, ?)
            ON CONFLICT(vk_user_id) DO UPDATE SET
                name = excluded.name,
                last_active = excluded.last_active
            """,
            (vk_user_id, name, time.time()),
        )
        await db.commit()


async def get_user_name(vk_user_id: int) -> str | None:
    async with aiosqlite.connect(_db_path) as db:
        async with db.execute(
            "SELECT name FROM users WHERE vk_user_id = ?", (vk_user_id,)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else None


async def list_users() -> list[VkUser]:
    """Все, кто когда-либо писал — для inline-кнопок выбора собеседника."""
    async with aiosqlite.connect(_db_path) as db:
        async with db.execute(
            "SELECT vk_user_id, name, last_active FROM users ORDER BY last_active DESC"
        ) as cur:
            rows = await cur.fetchall()
    return [VkUser(vk_user_id=r[0], name=r[1], last_active=r[2]) for r in rows]


async def set_last_recipient(vk_user_id: int) -> None:
    async with aiosqlite.connect(_db_path) as db:
        await db.execute(
            """
            INSERT INTO app_state (key, value) VALUES ('last_recipient', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (str(vk_user_id),),
        )
        await db.commit()


async def get_last_recipient() -> int | None:
    async with aiosqlite.connect(_db_path) as db:
        async with db.execute(
            "SELECT value FROM app_state WHERE key = 'last_recipient'"
        ) as cur:
            row = await cur.fetchone()
            return int(row[0]) if row else None


async def save_message_link(
    tg_message_id: int, vk_user_id: int, vk_message_id: int | None = None
) -> None:
    """Связать сообщение TG с собеседником и (опц.) id сообщения в ВК.

    vk_message_id нужен для нативных ответов: по нему находим, какому TG-сообщению
    соответствует сообщение ВК, и наоборот.
    """
    async with aiosqlite.connect(_db_path) as db:
        await db.execute(
            """
            INSERT INTO msg_links (tg_message_id, vk_user_id, vk_message_id, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(tg_message_id) DO UPDATE SET
                vk_user_id = excluded.vk_user_id,
                vk_message_id = COALESCE(excluded.vk_message_id, msg_links.vk_message_id)
            """,
            (tg_message_id, vk_user_id, vk_message_id, time.time()),
        )
        await db.commit()


async def get_vk_user_by_tg_message(tg_message_id: int) -> int | None:
    async with aiosqlite.connect(_db_path) as db:
        async with db.execute(
            "SELECT vk_user_id FROM msg_links WHERE tg_message_id = ?",
            (tg_message_id,),
        ) as cur:
            row = await cur.fetchone()
            return int(row[0]) if row else None


async def get_tg_by_vk_message(vk_message_id: int) -> int | None:
    """Найти TG-сообщение, соответствующее сообщению ВК (для нативного reply)."""
    async with aiosqlite.connect(_db_path) as db:
        async with db.execute(
            """
            SELECT tg_message_id FROM msg_links
            WHERE vk_message_id = ? ORDER BY created_at DESC LIMIT 1
            """,
            (vk_message_id,),
        ) as cur:
            row = await cur.fetchone()
            return int(row[0]) if row else None


async def get_vk_message_by_tg(tg_message_id: int) -> int | None:
    """id сообщения ВК для данного TG-сообщения (чтобы ответить нативно в ВК)."""
    async with aiosqlite.connect(_db_path) as db:
        async with db.execute(
            "SELECT vk_message_id FROM msg_links WHERE tg_message_id = ?",
            (tg_message_id,),
        ) as cur:
            row = await cur.fetchone()
            return int(row[0]) if row and row[0] is not None else None
