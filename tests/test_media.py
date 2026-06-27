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


def test_webp_to_png_conversion(tmp_path):
    src = tmp_path / "sticker.webp"
    Image.new("RGBA", (100, 100), (255, 0, 0, 255)).save(src, "WEBP")

    png_path = media._webp_to_png(str(src))

    assert png_path.endswith(".png")
    with Image.open(png_path) as im:
        assert im.format == "PNG"
        assert im.size == (100, 100)
