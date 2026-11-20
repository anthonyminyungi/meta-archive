"""Atomic JSON checkpoint store for resumable browser collection."""

from __future__ import annotations

import os
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from meta_archive_pipeline.utils.fs import atomic_write_bytes, to_long_path

ItemStatus = Literal["pending", "fetched", "media_saved", "done", "failed"]

_ALL_STATUSES: tuple[ItemStatus, ...] = ("pending", "fetched", "media_saved", "done", "failed")


class ItemRecord(BaseModel):
    """Checkpoint state for a single collectible item."""

    item_id: str
    status: ItemStatus = "pending"
    error: str | None = None
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    meta: dict[str, Any] = Field(default_factory=dict)


class CheckpointState(BaseModel):
    """Serializable root checkpoint document."""

    version: int = 1
    items: dict[str, ItemRecord] = Field(default_factory=dict)


class CheckpointStore:
    """Manages resumable item statuses persisted atomically to state.json."""

    def __init__(self, state_path: Path) -> None:
        self.state_path = state_path
        long_path = to_long_path(state_path)
        if os.path.exists(long_path):
            with open(long_path, "rb") as handle:
                self._state = CheckpointState.model_validate_json(handle.read())
        else:
            self._state = CheckpointState()

    def save(self) -> None:
        """Persist current checkpoint state atomically."""
        payload = self._state.model_dump_json(indent=2).encode("utf-8")
        atomic_write_bytes(self.state_path, payload)

    def ensure_items(self, item_ids: Iterable[str]) -> int:
        """Register unseen item IDs as pending and persist if any were added."""
        added = 0
        for item_id in item_ids:
            if item_id not in self._state.items:
                self._state.items[item_id] = ItemRecord(item_id=item_id)
                added += 1
        if added > 0:
            self.save()
        return added

    def get_status(self, item_id: str) -> ItemStatus | None:
        """Return current status for item_id, or None if unregistered."""
        record = self._state.items.get(item_id)
        return record.status if record is not None else None

    def get_record(self, item_id: str) -> ItemRecord | None:
        """Return full checkpoint record for item_id, or None if unregistered."""
        return self._state.items.get(item_id)

    def set_status(
        self,
        item_id: str,
        status: ItemStatus,
        *,
        error: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> ItemRecord:
        """Update status (and optional error/metadata) for item_id and persist atomically."""
        existing = self._state.items.get(item_id)
        merged_meta = dict(existing.meta) if existing else {}
        if meta:
            merged_meta.update(meta)

        record = ItemRecord(
            item_id=item_id,
            status=status,
            error=error,
            updated_at=datetime.now(timezone.utc),
            meta=merged_meta,
        )
        self._state.items[item_id] = record
        self.save()
        return record

    def ids_by_status(self, status: ItemStatus) -> list[str]:
        """Return item IDs matching the given status in insertion order."""
        return [
            item_id
            for item_id, record in self._state.items.items()
            if record.status == status
        ]

    def pending_ids(self, *, include_in_progress: bool = True) -> list[str]:
        """Return item IDs not yet marked `done` (or strictly `pending` if include_in_progress=False)."""
        if include_in_progress:
            return [
                item_id
                for item_id, record in self._state.items.items()
                if record.status != "done"
            ]
        return self.ids_by_status("pending")

    def summary(self) -> dict[str, int]:
        """Return counts per status across all tracked items."""
        counts: dict[str, int] = {status: 0 for status in _ALL_STATUSES}
        for record in self._state.items.values():
            counts[record.status] += 1
        return counts
