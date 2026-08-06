from __future__ import annotations

import io
from pathlib import Path
from typing import Any

from pypdf import PdfReader

from backend.core.config import Settings
from backend.documents.repository import DocumentRepository
from backend.persistence.files import SafeStorage


class FigureExtractor:
    def __init__(
        self,
        settings: Settings,
        storage: SafeStorage,
        repository: DocumentRepository,
    ) -> None:
        self.settings = settings
        self.storage = storage
        self.repository = repository

    def extract(self, pdf_path: Path, document_id: str) -> list[dict[str, Any]]:
        reader = PdfReader(str(pdf_path))
        figures: list[dict[str, Any]] = []
        for page_number, page in enumerate(reader.pages, start=1):
            figure_number = 0
            for embedded_image in page.images:
                image = embedded_image.image
                width, height = image.size
                if width < 128 or height < 128 or width * height < 40_000:
                    continue
                figure_number += 1
                output = io.BytesIO()
                image.save(output, format="PNG")
                relative_path = (
                    f"documents/{document_id}/figures/"
                    f"page-{page_number:04d}-figure-{figure_number:02d}.png"
                )
                stored = self.storage.write_bytes(
                    self.settings.artifacts_dir,
                    relative_path,
                    output.getvalue(),
                )
                artifact = self.repository.create_artifact(
                    owner_type="document",
                    kind="extracted_figure",
                    document_id=document_id,
                    relative_path=relative_path,
                    media_type="image/png",
                    stored=stored,
                    metadata={
                        "page": page_number,
                        "figure": figure_number,
                        "source_name": embedded_image.name,
                        "width": width,
                        "height": height,
                    },
                )
                figures.append(
                    {
                        "page": page_number,
                        "figure": figure_number,
                        "artifact_id": artifact.id,
                        "path": f"/api/artifacts/{artifact.id}/raw?sha256={artifact.sha256}",
                        "alt": (
                            f"Extracted figure {figure_number} from page {page_number}"
                        ),
                        "width": width,
                        "height": height,
                    }
                )
        return figures
