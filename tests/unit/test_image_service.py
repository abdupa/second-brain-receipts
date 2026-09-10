import logging
import random
import struct
import zlib
from dataclasses import FrozenInstanceError
from io import BytesIO

import pytest
from PIL import Image, ImageDraw, ImageFont, PngImagePlugin

from second_brain_receipts.core.config import Settings
from second_brain_receipts.services.image_service import (
    ImageErrorCode,
    ImageService,
    ImageValidationError,
    ProcessedImage,
)


def encode(image, fmt="PNG", **kwargs):
    output = BytesIO()
    image.save(output, format=fmt, **kwargs)
    return output.getvalue()


def make_image(size=(320, 240), mode="RGB", fmt="PNG", **kwargs):
    with Image.new(mode, size) as image:
        return encode(image, fmt, **kwargs)


@pytest.fixture(autouse=True)
def clean_settings_environment(monkeypatch):
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def service():
    return ImageService(Settings(app_env="test", _env_file=None))


def assert_error(service, raw, code):
    with pytest.raises(ImageValidationError) as exc:
        service.process(raw)
    assert exc.value.code is code
    assert str(exc.value) == code.value
    assert exc.value.__suppress_context__ or code in {
        ImageErrorCode.EMPTY,
        ImageErrorCode.TOO_LARGE,
        ImageErrorCode.DIMENSIONS,
        ImageErrorCode.TOO_SMALL,
        ImageErrorCode.UNSUPPORTED,
    }


@pytest.mark.parametrize(
    ("fmt", "mode"),
    [
        ("JPEG", "RGB"),
        ("PNG", "RGB"),
        ("WEBP", "RGB"),
        ("JPEG", "L"),
        ("PNG", "L"),
        ("PNG", "RGBA"),
        ("PNG", "LA"),
        ("PNG", "P"),
        ("JPEG", "CMYK"),
        ("PNG", "1"),
    ],
)
def test_supported_images_and_result(service, fmt, mode):
    raw = make_image(mode=mode, fmt=fmt)
    result = service.process(raw)
    assert result.mime_type == "image/jpeg"
    assert result.original_byte_size == len(raw)
    assert result.processed_byte_size == len(result.data)
    assert result.recompressed is True
    assert not result.resized
    assert (result.width, result.height) == (320, 240)
    with Image.open(BytesIO(result.data)) as decoded:
        decoded.load()
        assert decoded.format == "JPEG"
        assert decoded.mode == "RGB"
        assert decoded.size == (result.width, result.height)
    assert "data=" not in repr(result)
    with pytest.raises(FrozenInstanceError):
        result.width = 1


@pytest.mark.parametrize("mode", ["RGBA", "LA", "P", "RGB"])
def test_transparency_composited_on_white(service, mode):
    with Image.new(mode, (320, 240)) as source:
        options = {"transparency": 0} if mode == "P" else {}
        if mode == "RGB":
            options = {"transparency": (0, 0, 0)}
        if mode == "RGBA":
            source.paste((200, 0, 0, 128), (160, 0, 320, 240))
        result = service.process(encode(source, **options))
    with Image.open(BytesIO(result.data)) as output:
        assert min(output.getpixel((20, 20))) >= 250
        if mode == "RGBA":
            red, green, blue = output.getpixel((240, 20))
            assert 220 <= red <= 235 and 120 <= green <= 135 and 120 <= blue <= 135


def test_empty_and_non_image(service):
    assert_error(service, b"", ImageErrorCode.EMPTY)
    assert_error(service, b"not an image", ImageErrorCode.UNSUPPORTED)


@pytest.mark.parametrize("fmt", ["BMP", "GIF", "TIFF"])
def test_unsupported_formats(service, fmt):
    assert_error(service, make_image(fmt=fmt), ImageErrorCode.UNSUPPORTED)


@pytest.mark.parametrize("fmt", ["PNG", "WEBP"])
def test_animation_rejected(service, fmt):
    with Image.new("RGB", (320, 240), "white") as first:
        with Image.new("RGB", first.size, "black") as second:
            raw = encode(first, fmt, save_all=True, append_images=[second], duration=100, loop=0)
    assert_error(service, raw, ImageErrorCode.UNSUPPORTED)


@pytest.mark.parametrize("fmt", ["JPEG", "PNG", "WEBP"])
def test_truncated_input_with_real_header(service, fmt):
    raw = make_image(fmt=fmt)
    with pytest.raises(ImageValidationError) as exc:
        service.process(raw[: len(raw) // 2])
    assert exc.value.code in {ImageErrorCode.CORRUPT, ImageErrorCode.UNSUPPORTED}


def test_jpeg_missing_end_marker_fails_full_decode(service):
    raw = make_image(fmt="JPEG")
    # JPEG verify() alone accepts this; full decode must still reject it.
    with Image.open(BytesIO(raw[:-2])) as image:
        image.verify()
    assert_error(service, raw[:-2], ImageErrorCode.CORRUPT)


def test_png_corruption_after_header(service):
    raw = bytearray(make_image())
    start = raw.index(b"IDAT")
    raw[start + 5] ^= 0xFF  # valid header; damaged compressed content/checksum
    assert_error(service, bytes(raw), ImageErrorCode.CORRUPT)


def test_byte_limit_before_decoder(monkeypatch):
    service = ImageService(Settings(app_env="test", _env_file=None, max_upload_bytes=10))

    def forbidden(*args, **kwargs):
        pytest.fail("decoder must not run for oversized input")

    monkeypatch.setattr(Image, "open", forbidden)
    assert_error(service, b"x" * 11, ImageErrorCode.TOO_LARGE)


def test_exact_upload_limit_accepted():
    raw = make_image()
    service = ImageService(Settings(app_env="test", _env_file=None, max_upload_bytes=len(raw)))
    assert service.process(raw).original_byte_size == len(raw)


def with_png_dimensions(raw, width, height):
    data = bytearray(raw)
    data[16:24] = struct.pack(">II", width, height)
    data[29:33] = struct.pack(">I", zlib.crc32(data[12:29]))
    return bytes(data)


@pytest.mark.parametrize(("width", "height"), [(10_000, 5_000), (20_001, 64)])
def test_pathological_dimensions_rejected_before_decode(service, monkeypatch, width, height):
    raw = with_png_dimensions(make_image(), width, height)

    def forbidden(*args, **kwargs):
        pytest.fail("full decode must not run for unsafe dimensions")

    monkeypatch.setattr(Image.Image, "load", forbidden)
    assert_error(service, raw, ImageErrorCode.DIMENSIONS)


@pytest.mark.parametrize("pillow_limit", [50_000, 10_000])
def test_pillow_bomb_warning_and_error_are_typed(service, monkeypatch, pillow_limit):
    raw = make_image()
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", pillow_limit)
    assert_error(service, raw, ImageErrorCode.DIMENSIONS)
    assert Image.MAX_IMAGE_PIXELS == pillow_limit


def test_configured_pixel_limit():
    service = ImageService(Settings(app_env="test", _env_file=None, image_max_pixels=20_000))
    assert_error(service, make_image(), ImageErrorCode.DIMENSIONS)


@pytest.mark.parametrize("size", [(1, 1), (63, 1000), (100, 100), (200, 50)])
def test_too_small(service, size):
    assert_error(service, make_image(size), ImageErrorCode.TOO_SMALL)


@pytest.mark.parametrize(
    "size",
    [
        (4032, 3024),
        (3024, 4032),
        (2001, 777),
        (777, 2001),
        (320, 240),
        (128, 128),
        (120, 1800),
        (1600, 1000),
    ],
)
def test_resize_invariants(service, size):
    result = service.process(make_image(size))
    assert max(result.width, result.height) <= 1600
    assert result.width <= size[0] and result.height <= size[1]
    assert result.resized == (max(size) > 1600)
    scale = min(1, 1600 / max(size))
    assert abs(result.width - size[0] * scale) <= 1
    assert abs(result.height - size[1] * scale) <= 1
    if max(size) <= 1600:
        assert (result.width, result.height) == size


def test_extreme_aspect_ratio_does_not_return_unusable_sliver(service):
    assert_error(service, make_image((64, 4000)), ImageErrorCode.TOO_SMALL)


def test_custom_long_edge_and_quality():
    raw = make_image((2000, 1500))
    service = ImageService(
        Settings(app_env="test", _env_file=None, image_max_long_edge=1000, image_jpeg_quality=82)
    )
    result = service.process(raw)
    assert (result.width, result.height) == (1000, 750)
    with Image.open(BytesIO(result.data)) as output:
        # Compare JPEG quantization tables, not unstable encoded byte counts.
        with Image.open(BytesIO(make_image((1000, 750), fmt="JPEG", quality=82))) as expected:
            assert output.quantization == expected.quantization


def synthetic_receipt():
    """Deterministic camera-sized receipt with texture, shading and readable text."""
    rng = random.Random(7)
    texture = bytes(rng.randrange(220, 256) for _ in range(1008 * 756))
    with Image.frombytes("L", (1008, 756), texture) as noise:
        image = noise.resize((4032, 3024), Image.Resampling.BICUBIC).convert("RGB")
    draw = ImageDraw.Draw(image)
    draw.rectangle((550, 140, 3450, 2850), fill="white", outline="gray", width=4)
    font = ImageFont.load_default(size=72)
    small_font = ImageFont.load_default(size=42)
    draw.text((720, 300), "SYNTHETIC RECEIPT - ABC HARDWARE", font=font, fill="black")
    for index, text in enumerate(
        [
            "10/09/2026          Receipt # TEST-003",
            "Item                       Amount PHP",
            "Maintenance supplies             100.50",
            "Utilities                       2500.00",
            "Food supplies                    399.99",
            "VAT                              321.48",
            "TOTAL                           3000.49",
            "Thank you - generated test fixture",
        ]
    ):
        draw.text((720, 550 + 220 * index), text, font=font, fill="black")
    draw.text(
        (720, 2550), "Small print: quantities 1.00 / 2.50 / 10.00", font=small_font, fill="black"
    )
    return image


def test_representative_compression_and_determinism(service):
    with synthetic_receipt() as source:
        raw = encode(source, "JPEG", quality=98, subsampling=0)
    result = service.process(raw)
    assert len(raw) < 10 * 1024 * 1024
    assert result.processed_byte_size < result.original_byte_size // 2
    assert result.data == service.process(raw).data
    with Image.open(BytesIO(result.data)) as output:
        output.load()
        assert output.size == (1600, 1200)


@pytest.mark.parametrize(
    ("orientation", "expected_size", "red_corner"),
    [
        (6, (240, 320), (220, 20)),
        (8, (240, 320), (20, 300)),
        (3, (320, 240), (300, 220)),
    ],
)
def test_exif_orientation_and_privacy(service, orientation, expected_size, red_corner):
    exif = Image.Exif()
    exif[274] = orientation
    exif[271] = "PRIVATE DEVICE"
    exif[272] = "PRIVATE MODEL"
    exif[306] = "2026:09:10 12:34:56"
    exif[34853] = {1: "N", 2: (1.0, 2.0, 3.0)}
    with Image.new("RGB", (320, 240), "white") as source:
        ImageDraw.Draw(source).rectangle((0, 0, 100, 100), fill="red")
        raw = encode(
            source, "JPEG", exif=exif, comment=b"PRIVATE COMMENT", icc_profile=b"PRIVATE ICC"
        )
    result = service.process(raw)
    assert not result.resized  # rotation alone is not resizing
    assert (result.width, result.height) == expected_size
    with Image.open(BytesIO(result.data)) as output:
        assert not output.getexif()
        assert "icc_profile" not in output.info
        assert "comment" not in output.info
        red, green, blue = output.getpixel(red_corner)
        assert red > 200 and green < 30 and blue < 30
    for marker in (b"PRIVATE", b"Exif", b"2026:09:10"):
        assert marker not in result.data


def test_orientation_before_resize(service):
    exif = Image.Exif()
    exif[274] = 6
    result = service.process(make_image((2400, 1800), fmt="JPEG", exif=exif))
    assert (result.width, result.height) == (1200, 1600)


def test_png_text_not_carried_to_output(service):
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("private", "DO NOT RETAIN")
    result = service.process(make_image(pnginfo=metadata))
    assert b"DO NOT RETAIN" not in result.data


def test_safe_service_logging(service, caplog):
    caplog.set_level(logging.DEBUG, logger="second_brain_receipts.services.image_service")
    raw = make_image()
    result = service.process(raw)
    records = [r for r in caplog.records if r.name.endswith("image_service")]
    assert len(records) == 1
    record = records[0]
    assert record.message == "image_processed"
    assert record.original_byte_size == len(raw)
    assert record.processed_byte_size == len(result.data)
    assert record.image_format == "PNG"
    assert not any(isinstance(value, bytes) for value in vars(record).values())


def test_invalid_result_metadata():
    with pytest.raises(ValueError):
        ProcessedImage(data=b"", width=1, height=1, original_byte_size=1, resized=False)


def test_processing_has_no_disk_io(service, monkeypatch):
    import builtins
    import tempfile

    raw = make_image()
    Image.init()  # Plugin imports are startup work, not per-image file persistence.

    def forbidden(*args, **kwargs):
        pytest.fail("image processing must stay in memory")

    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(tempfile, "mkstemp", forbidden)
    assert service.process(raw).data
