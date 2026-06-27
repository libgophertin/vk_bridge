import tg_handler


def test_plural():
    assert tg_handler._plural(1) == "сообщение"
    assert tg_handler._plural(2) == "сообщения"
    assert tg_handler._plural(3) == "сообщения"
    assert tg_handler._plural(5) == "сообщений"
    assert tg_handler._plural(11) == "сообщений"
    assert tg_handler._plural(21) == "сообщение"
    assert tg_handler._plural(112) == "сообщений"


def test_compose_keyboard_buttons():
    kb = tg_handler._compose_keyboard()
    # Теперь это reply-клавиатура (постоянная, снизу), а не inline.
    flat = [b.text for row in kb.keyboard for b in row]
    assert tg_handler.BTN_SEND in flat
    assert tg_handler.BTN_CLEAR in flat
    assert tg_handler.BTN_SHOW in flat
    assert kb.is_persistent is True
