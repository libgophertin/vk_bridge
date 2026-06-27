from PIL import Image

import media


def test_largest_image_url_picks_biggest():
    images = [
        {"url": "small", "width": 64, "height": 64},
        {"url": "big", "width": 512, "height": 512},
        {"url": "mid", "width": 128, "height": 128},
    ]
    assert media._largest_image_url(images) == "big"


def test_largest_image_url_empty():
    assert media._largest_image_url([]) is None


def test_strip_sender_header():
    # старый формат со ссылкой в скобках
    assert media._strip_sender_header("👤 Иван Петров (vk.com/id5): привет") == "привет"
    # новый «красивый» формат без скобок
    assert media._strip_sender_header("👤 Иван Петров: привет") == "привет"
    assert media._strip_sender_header("✏️ Иван изменил(а): правка") == "правка"
    assert media._strip_sender_header("обычный текст") == "обычный текст"


def test_clean_reply_text_plain():
    assert media.clean_reply_text("привет") == "привет"
    assert media.clean_reply_text("") == ""


def test_clean_reply_text_strips_header():
    assert media.clean_reply_text("👤 Иван (vk.com/id5): здаров") == "здаров"


def test_clean_reply_text_strips_nested_quote():
    # старый формат с вложенной цитатой — оставляем только сам текст ответа
    assert media.clean_reply_text("«привет»\n\nздаров") == "здаров"


def test_clean_reply_text_folded_reply_format():
    # сообщение бота с шапкой на отдельной строке и контекстом ↩️ —
    # достаём только сам текст, без шапки и без вложенного ответа
    raw = "👤 Имя Фамилия:\n↩️ qwd\n\nПривет"
    assert media.clean_reply_text(raw) == "Привет"


def test_clean_reply_text_drops_nested_resend():
    # «↩️ qwd» в начале — это контекст, его убираем, оставляя сам ответ
    assert media.clean_reply_text("↩️ qwd\n\nПривет, как дела?") == "Привет, как дела?"


def test_clean_reply_text_truncates():
    out = media.clean_reply_text("a" * 500)
    assert out.endswith("…")
    assert len(out) <= media.CONTEXT_MAX_LEN + 1


def test_webp_to_png_conversion(tmp_path):
    src = tmp_path / "sticker.webp"
    Image.new("RGBA", (100, 100), (255, 0, 0, 255)).save(src, "WEBP")

    png_path = media._webp_to_png(str(src))

    assert png_path.endswith(".png")
    with Image.open(png_path) as im:
        assert im.format == "PNG"
        assert im.size == (100, 100)
