"""Bounded, in-memory receipt image validation and JPEG optimization."""

import logging
import struct
import warnings
from contextlib import ExitStack
from dataclasses import dataclass, field
from enum import StrEnum
from io import BytesIO
from typing import Literal

from PIL import Image, ImageOps, UnidentifiedImageError

from second_brain_receipts.core.config import Settings

logger = logging.getLogger(__name__)
SUPPORTED_FORMATS = ("JPEG", "PNG", "WEBP")
MAX_RAW_EDGE = 20_000
MIN_EDGE = 64
MIN_PIXELS = 16_384


class ImageErrorCode(StrEnum):
    EMPTY = "empty_image"
    UNSUPPORTED = "unsupported_image"
    CORRUPT = "corrupt_image"
    TOO_LARGE = "image_too_large"
    DIMENSIONS = "unsafe_image_dimensions"
    TOO_SMALL = "image_too_small"


class ImageValidationError(ValueError):
    """Safe machine-readable reason, without source bytes or decoder messages."""

    def __init__(self, code: ImageErrorCode) -> None:
        self.code = code
        super().__init__(code.value)


@dataclass(frozen=True, slots=True)
class ProcessedImage:
    """JPEG result; sizes are bytes, and dimensions describe the oriented output."""

    data: bytes = field(repr=False)
    width: int
    height: int
    original_byte_size: int
    resized: bool
    mime_type: Literal["image/jpeg"] = field(default="image/jpeg", init=False)
    recompressed: Literal[True] = field(default=True, init=False)

    def __post_init__(self) -> None:
        if not self.data or min(self.width, self.height, self.original_byte_size) <= 0:
            raise ValueError("processed image requires bytes and positive dimensions/sizes")

    @property
    def processed_byte_size(self) -> int:
        return len(self.data)


class ImageService:
    """Uses explicitly supplied settings; no environment or provider access here."""

    def __init__(self, settings: Settings) -> None:
        self._max_upload_bytes = settings.max_upload_bytes
        self._max_pixels = settings.image_max_pixels
        self._max_long_edge = settings.image_max_long_edge
        self._jpeg_quality = settings.image_jpeg_quality

    def process(self, raw_bytes: bytes) -> ProcessedImage:
        if not raw_bytes:
            raise ImageValidationError(ImageErrorCode.EMPTY)
        if len(raw_bytes) > self._max_upload_bytes:
            raise ImageValidationError(ImageErrorCode.TOO_LARGE)

        try:
            # Local filters only; never change Pillow's global limits or truncated-file policy.
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                # Pillow can warn about malformed EXIF/PNG metadata instead of raising.
                warnings.simplefilter("error", UserWarning)
                return self._process_verified(raw_bytes)
        except ImageValidationError:
            raise
        except (Image.DecompressionBombWarning, Image.DecompressionBombError):
            raise ImageValidationError(ImageErrorCode.DIMENSIONS) from None
        except UnidentifiedImageError:
            raise ImageValidationError(ImageErrorCode.UNSUPPORTED) from None
        except (
            OSError,
            ValueError,
            SyntaxError,
            EOFError,
            struct.error,
            OverflowError,
            UserWarning,
        ):
            raise ImageValidationError(ImageErrorCode.CORRUPT) from None

    def _check_dimensions(self, width: int, height: int) -> None:
        if max(width, height) > MAX_RAW_EDGE or width * height > self._max_pixels:
            raise ImageValidationError(ImageErrorCode.DIMENSIONS)
        if min(width, height) < MIN_EDGE or width * height < MIN_PIXELS:
            raise ImageValidationError(ImageErrorCode.TOO_SMALL)

    def _process_verified(self, raw_bytes: bytes) -> ProcessedImage:
        # Restrict decoders, rather than probing arbitrary Pillow formats.
        with Image.open(BytesIO(raw_bytes), formats=SUPPORTED_FORMATS) as probe:
            if probe.format not in SUPPORTED_FORMATS or getattr(probe, "n_frames", 1) != 1:
                raise ImageValidationError(ImageErrorCode.UNSUPPORTED)
            self._check_dimensions(*probe.size)
            original_size = probe.size
            source_format = probe.format
            probe.verify()

        # verify() consumes/invalidates the probe. Reopen and fully decode before transforms.
        with ExitStack() as stack:
            source = stack.enter_context(Image.open(BytesIO(raw_bytes), formats=SUPPORTED_FORMATS))
            source.load()
            oriented = stack.enter_context(ImageOps.exif_transpose(source))
            oriented_size = oriented.size
            if "A" in oriented.getbands() or "transparency" in oriented.info:
                rgba = stack.enter_context(oriented.convert("RGBA"))
                rgb = stack.enter_context(Image.new("RGB", oriented.size, "white"))
                alpha = stack.enter_context(rgba.getchannel("A"))
                rgb.paste(rgba, mask=alpha)
            else:
                rgb = stack.enter_context(oriented.convert("RGB"))

            rgb.thumbnail(
                (self._max_long_edge, self._max_long_edge),
                resample=Image.Resampling.LANCZOS,
                reducing_gap=3.0,
            )
            # Do not send a sliver made unreadable by a very extreme aspect ratio.
            self._check_dimensions(*rgb.size)
            resized = rgb.size != oriented_size
            # Pillow can inherit comments and profiles through conversion: remove all info.
            rgb.info.clear()
            output = BytesIO()
            rgb.save(
                output,
                format="JPEG",
                quality=self._jpeg_quality,
                optimize=True,
                subsampling=0,
                exif=b"",
                icc_profile=None,
            )
            result = ProcessedImage(
                data=output.getvalue(),
                width=rgb.width,
                height=rgb.height,
                original_byte_size=len(raw_bytes),
                resized=resized,
            )
        logger.debug(
            "image_processed",
            extra={
                "original_byte_size": result.original_byte_size,
                "processed_byte_size": result.processed_byte_size,
                "original_width": original_size[0],
                "original_height": original_size[1],
                "processed_width": result.width,
                "processed_height": result.height,
                "image_format": source_format,
                "resized": result.resized,
            },
        )
        return result
