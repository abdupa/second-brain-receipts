"""Application-facing transport values, independent of Telegram wire schemas."""

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True, slots=True)
class IncomingMessage:
    update_id: int
    chat_id: int
    user_id: int
    message_id: int
    received_at: datetime


@dataclass(frozen=True, slots=True)
class IncomingPhotoMessage(IncomingMessage):
    telegram_file_id: str = field(repr=False)
    image_bytes: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class IncomingTextMessage(IncomingMessage):
    text: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class UnsupportedMessage(IncomingMessage):
    pass


IncomingEvent = IncomingPhotoMessage | IncomingTextMessage | UnsupportedMessage
