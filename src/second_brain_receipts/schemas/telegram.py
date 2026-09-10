"""Minimal external Telegram wire schemas; unused provider fields are ignored."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

Identifier = Annotated[int, Field(strict=True, ge=0, le=2**63 - 1)]
ChatIdentifier = Annotated[int, Field(strict=True, ge=-(2**63), le=2**63 - 1)]


class TelegramModel(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, hide_input_in_errors=True)


class TelegramUser(TelegramModel):
    id: Identifier


class TelegramChat(TelegramModel):
    id: ChatIdentifier
    type: str


class TelegramPhoto(TelegramModel):
    file_id: str = Field(min_length=1, max_length=2048, repr=False)
    width: int = Field(strict=True, gt=0)
    height: int = Field(strict=True, gt=0)
    file_size: int | None = Field(default=None, strict=True, ge=0)


class TelegramMessage(TelegramModel):
    message_id: Identifier
    sender: TelegramUser | None = Field(default=None, alias="from")
    chat: TelegramChat
    photo: list[TelegramPhoto] = Field(default_factory=list)
    text: str | None = Field(default=None, max_length=4096, repr=False)


class TelegramUpdate(TelegramModel):
    update_id: Identifier
    message: TelegramMessage | None = None
