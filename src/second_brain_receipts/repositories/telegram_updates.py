"""Atomic INSERT ON CONFLICT DO NOTHING claims; no read-before-write race."""

from typing import Literal, Protocol

from pydantic import AwareDatetime, BaseModel, TypeAdapter
from supabase import AsyncClient

from second_brain_receipts.repositories.errors import InvalidPersistenceResponseError
from second_brain_receipts.repositories.supabase import _one, _rows
from second_brain_receipts.schemas.telegram import Identifier

UpdateStatus = Literal["received", "handled", "failed"]


class UpdateRecord(BaseModel):
    update_id: Identifier
    received_at: AwareDatetime
    status: UpdateStatus


class UpdateRepository(Protocol):
    async def claim_update(self, update_id: int) -> bool: ...
    async def finish_update(self, update_id: int, status: Literal["handled", "failed"]) -> None: ...


class SupabaseUpdateRepository:
    def __init__(self, client: AsyncClient) -> None:
        self._client = client

    async def claim_update(self, update_id: int) -> bool:
        TypeAdapter(Identifier).validate_python(update_id)
        rows = await _rows(
            self._client.table("telegram_updates")
            .upsert(
                {"update_id": update_id},
                on_conflict="update_id",
                ignore_duplicates=True,
            )
            .select("update_id,received_at,status"),
            UpdateRecord,
        )
        if not rows:
            return False
        row = _one(rows)
        if row.update_id != update_id or row.status != "received":
            raise InvalidPersistenceResponseError()
        return True

    async def finish_update(self, update_id: int, status: Literal["handled", "failed"]) -> None:
        if status not in ("handled", "failed"):
            raise ValueError("invalid terminal update status")
        row = _one(
            await _rows(
                self._client.table("telegram_updates")
                .update({"status": status})
                .eq("update_id", update_id)
                .eq("status", "received")
                .select("update_id,received_at,status"),
                UpdateRecord,
            )
        )
        if row.update_id != update_id or row.status != status:
            raise InvalidPersistenceResponseError()
