"""Receipt extraction contract and safe provider-independent failures."""

from typing import Protocol

from second_brain_receipts.schemas.receipts import ReceiptExtraction
from second_brain_receipts.services.image_service import ProcessedImage


class ReceiptVisionProvider(Protocol):
    async def extract(self, image: ProcessedImage) -> ReceiptExtraction: ...


class VisionProviderError(Exception):
    code = "vision_provider_error"
    transient = False

    def __init__(self) -> None:
        super().__init__(self.code)


class VisionAuthenticationError(VisionProviderError):
    code = "vision_authentication_error"


class VisionRateLimitError(VisionProviderError):
    code = "vision_rate_limit"
    transient = True


class VisionTimeoutError(VisionProviderError):
    code = "vision_timeout"
    transient = True


class VisionUnavailableError(VisionProviderError):
    code = "vision_unavailable"
    transient = True


class VisionResponseError(VisionProviderError):
    code = "vision_invalid_response"


class ReceiptExtractionError(VisionProviderError):
    code = "receipt_unextractable"
