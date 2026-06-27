import database as db


async def _fresh_db(tmp_path):
    db.configure(str(tmp_path / "test.db"))
    await db.init_db()


async def test_upsert_and_get_user(tmp_path):
    await _fresh_db(tmp_path)
    await db.upsert_user(123, "Иван Иванов")
    assert await db.get_user_name(123) == "Иван Иванов"

    # Повторный upsert обновляет имя, не плодит дубликаты.
    await db.upsert_user(123, "Иван Петров")
    assert await db.get_user_name(123) == "Иван Петров"
    users = await db.list_users()
    assert len(users) == 1


async def test_list_users_sorted_by_activity(tmp_path):
    await _fresh_db(tmp_path)
    await db.upsert_user(1, "Первый")
    await db.upsert_user(2, "Второй")
    # Делаем первого снова активным — он должен оказаться сверху.
    await db.upsert_user(1, "Первый")
    users = await db.list_users()
    assert users[0].vk_user_id == 1


async def test_last_recipient(tmp_path):
    await _fresh_db(tmp_path)
    assert await db.get_last_recipient() is None
    await db.set_last_recipient(555)
    assert await db.get_last_recipient() == 555
    await db.set_last_recipient(777)
    assert await db.get_last_recipient() == 777


async def test_message_links(tmp_path):
    await _fresh_db(tmp_path)
    assert await db.get_vk_user_by_tg_message(10) is None
    await db.save_message_link(10, 42)
    assert await db.get_vk_user_by_tg_message(10) == 42
