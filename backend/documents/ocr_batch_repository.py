from __future__ import annotations

import json
from dataclasses import asdict

from backend.documents.ocr_batch_models import OcrBatch, OcrBatchItem
from backend.persistence.files import SafeStorage


class OcrBatchRepository:
    """Small local job receipts, independent of the document database schema."""

    def __init__(self, storage: SafeStorage) -> None:
        self.storage = storage

    def list(self) -> list[OcrBatch]:
        base = self.storage.settings.data_dir
        folder = self.storage.resolve_path(base, "ocr-batches")
        batches = []
        for path in folder.glob("*.json"):
            raw = json.loads(self.storage.read_text(base, f"ocr-batches/{path.name}"))
            raw["items"] = [OcrBatchItem(**item) for item in raw["items"]]
            batches.append(OcrBatch(**raw))
        return sorted(batches, key=lambda batch: batch.created_at, reverse=True)

    def save(self, batch: OcrBatch) -> None:
        self.storage.write_bytes_atomic(
            self.storage.settings.data_dir,
            f"ocr-batches/{batch.id}.json",
            json.dumps(asdict(batch), ensure_ascii=False).encode("utf-8"),
        )
