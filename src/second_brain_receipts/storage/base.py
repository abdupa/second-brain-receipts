"""Provider-independent image storage contract and safe errors."""

from dataclasses import dataclass
from typing import Literal, Protocol

from second_brain_receipts.services.image_service import ProcessedImage


@dataclass(frozen=True, slots=True)
class StoredReceiptImage:
    object_key: str
    storage_uri: str
    bucket: str
    size_bytes: int
    content_type: Literal["image/jpeg"] = "image/jpeg"


class ReceiptImageStorage(Protocol):
    async def store(self, image: ProcessedImage) -> StoredReceiptImage: ...
    async def delete(self, object_key: str) -> None: ...
    async def create_download_url(self, object_key: str) -> str: ...


class StorageError(Exception):
    code = "storage_error"
    transient = False

    def __init__(self, *, object_key: str | None = None) -> None:
        self.object_key = object_key  # Opaque key allows later reconciliation of uncertain uploads.
        super().__init__(self.code)


class StorageAuthenticationError(StorageError):
    code = "storage_authentication_error"


class StorageUnavailableError(StorageError):
    code = "storage_unavailable"
    transient = True


class StorageUploadError(StorageError):
    code = "storage_upload_error"


class StorageDeleteError(StorageError):
    code = "storage_delete_error"


class StoragePresignError(StorageError):
    code = "storage_presign_error"


class StorageAmbiguousError(StorageError):
    code = "storage_upload_ambiguous"


@dataclass(frozen=True, slots=True)
class StoredObjectInfo:
    object_key: str
    size_bytes: int
    content_type: str
