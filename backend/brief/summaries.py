from __future__ import annotations

import hashlib
import asyncio
import json
import math
import uuid
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from backend.agents.blueprint import ModelReferenceSpec
from backend.agents.context_budget import model_context_budget
from backend.agents.input_budget import InputBudget
from backend.brief.schemas import BriefEditionResponse, BriefHighlightSummary
from backend.core.errors import ValidationError
from backend.persistence.files import SafeStorage
from backend.prompting.registry import PromptRegistry
from backend.providers.runtime import ModelRuntime
from backend.providers.types import ModelReference
from backend.utils import utcnow
from backend.workspace.service import WorkspaceService

MAX_INPUT_TOKENS = 32_768
TEMP_FOLDER = ".scholarweave/brief-temp"
ARCHIVE_TAG = "morning-brief-summary"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class BriefSummaryService:
    def __init__(
        self, storage: SafeStorage, workspace: WorkspaceService,
        runtime: ModelRuntime, prompts: PromptRegistry,
    ) -> None:
        self.storage = storage
        self.workspace = workspace
        self.runtime = runtime
        self.prompts = prompts

    @staticmethod
    def records(edition: BriefEditionResponse) -> list[dict[str, Any]]:
        return [{
            "title": item.title, "abstract": item.abstract, "kind": item.kind,
            "publisher": item.source_name, "authors": item.authors, "topics": item.topics,
            "published_at": item.published_at.isoformat() if item.published_at else None,
            "announcement": item.announcement,
        } for item in edition.items]

    @classmethod
    def source_hash(cls, edition: BriefEditionResponse) -> str:
        return hashlib.sha256(_json(cls.records(edition)).encode("utf-8")).hexdigest()

    def default_reference(self) -> ModelReferenceSpec:
        settings = self.runtime.settings
        preferred = settings.last_chat_model_reference
        reference = (
            preferred if preferred.get("provider_profile_id") and preferred.get("model")
            else settings.default_model_references.get("chat", {})
        )
        return ModelReferenceSpec.model_validate(reference)

    def clear_temporary(self) -> None:
        self.storage.delete_stored_tree(self.storage.settings.workspace_dir, TEMP_FOLDER)

    def save(self, summary: BriefHighlightSummary) -> None:
        self.workspace.write_file(
            summary.workspace_path, summary.model_dump(mode="json"),
            tags=[ARCHIVE_TAG, f"brief-day:{summary.day}"],
        )

    def history(self, *, limit: int, offset: int) -> list[BriefHighlightSummary]:
        summaries: list[BriefHighlightSummary] = []
        for item in self.workspace.search(tags=[ARCHIVE_TAG], limit=limit, offset=offset):
            try:
                summaries.append(BriefHighlightSummary.model_validate(self.workspace.read_file(item.path).content))
            except (ValueError, OSError) as exc:
                raise ValidationError(f"Could not read daily highlights archive {item.path}: {exc}") from exc
        return summaries

    async def generate(
        self, edition: BriefEditionResponse, reference: ModelReferenceSpec,
        progress: Callable[[int, int], Awaitable[None]],
    ) -> BriefHighlightSummary:
        if not edition.day or not edition.items:
            raise ValidationError("Collect headlines before generating highlights.")
        resolved = self.runtime.resolve("chat", model_reference=ModelReference(
            reference.provider_profile_id, reference.model,
        ))
        window = resolved.context_window_tokens or self.runtime.settings.agent_context_window_tokens
        window = min(window, math.ceil((MAX_INPUT_TOKENS + 4096) / 0.9))
        input_limit, output_limit, _ = model_context_budget(window, min(4096, window // 4))
        budget = await asyncio.to_thread(InputBudget, resolved.model, min(MAX_INPUT_TOKENS, input_limit))
        system = self.prompts.render("brief-highlights")
        combine_system = self.prompts.render("brief-highlights-combine")
        batches = await asyncio.to_thread(budget.batches, self.records(edition), system)
        total = len(batches)
        completed = 0
        run_folder = f"{TEMP_FOLDER}/{edition.day}/{uuid.uuid4().hex}"
        await progress(completed, total)

        async def call(records: list[Any], instructions: str, *, final: bool) -> str:
            prompt = _json({"stage": "final" if final else "part", "records": records})
            if await asyncio.to_thread(budget.count, instructions, prompt) > budget.limit:
                raise ValidationError("Morning Brief request exceeds its input token budget.")
            response = await self.runtime.generate(
                resolved, prompt, system=instructions, think=False,
                max_output_tokens=output_limit,
                context_window_tokens=window,
            )
            text = response.get("response") or response.get("message", {}).get("content") or ""
            raw = response.get("raw") or {}
            if any(choice.get("finish_reason") in {"length", "content_filter"} for choice in raw.get("choices", [])):
                raise ValidationError("The highlights response was truncated or filtered. Retry with another model.")
            if response.get("done_reason") == "length" or not isinstance(text, str) or not text.strip():
                raise ValidationError("The model did not return a complete highlights summary. Try another model.")
            return text.strip()

        if total == 1:
            text = await call(batches[0], system, final=True)
            await progress(1, 1)
        else:
            parts: list[str] = []
            for index, batch in enumerate(batches, start=1):
                part = await call(batch, system, final=False)
                self.storage.write_bytes_atomic(
                    self.storage.settings.workspace_dir, f"{run_folder}/part-{index:04d}.json",
                    _json({"part": index, "item_count": len(batch), "text": part}).encode("utf-8"),
                )
                parts.append(part)
                completed += 1
                await progress(completed, total)
            level = 0
            while True:
                groups = await asyncio.to_thread(budget.batches, parts, combine_system)
                if len(groups) == 1:
                    total += 1
                    await progress(completed, total)
                    text = await call(groups[0], combine_system, final=True)
                    completed += 1
                    await progress(completed, total)
                    break
                if len(groups) >= len(parts):
                    raise ValidationError("Summary parts cannot be combined within this model's input budget.")
                level += 1
                total += len(groups)
                await progress(completed, total)
                reduced: list[str] = []
                for index, group in enumerate(groups, start=1):
                    part = await call(group, combine_system, final=False)
                    self.storage.write_bytes_atomic(
                        self.storage.settings.workspace_dir, f"{run_folder}/combine-{level}-{index:04d}.json",
                        _json({"text": part}).encode("utf-8"),
                    )
                    reduced.append(part)
                    completed += 1
                    await progress(completed, total)
                parts = reduced
        return BriefHighlightSummary(
            day=edition.day, text=text, created_at=utcnow(),
            model_reference=ModelReferenceSpec(provider_profile_id=resolved.profile_id, model=resolved.model),
            source_hash=self.source_hash(edition), item_count=len(edition.items), batch_count=len(batches),
            input_token_limit=budget.limit, token_counting=budget.method,
            workspace_path=f"knowledge/morning-brief/{edition.day}.json",
        )
