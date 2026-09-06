from __future__ import annotations

import base64
import json
import re
import time
from typing import Any

from openai import OpenAIError

from backend.core.config import Settings
from backend.core.errors import DocumentProcessingError
from backend.documents.formatting import DocumentFormatter
from backend.providers.types import ProviderRuntimeError
from backend.utils import ProgressCallback, report_progress
from backend.providers.ollama import OllamaClient, OllamaError
from backend.providers.runtime import ModelRuntime, ResolvedModel
from backend.providers.types import AgentModelDefaults, ModelReference
from backend.prompting.registry import PromptRegistry


OCR_QUALITY_LEVELS = ("good", "average", "poor")

OCR_QUALITY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "quality": {"type": "string", "enum": list(OCR_QUALITY_LEVELS)},
        "issues": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["quality", "issues"],
    "additionalProperties": False,
}


class VisionEnhancer:
    def __init__(
        self,
        settings: Settings,
        ollama: OllamaClient,
        model_runtime: ModelRuntime,
        formatter: DocumentFormatter,
        prompts: PromptRegistry | None = None,
    ) -> None:
        self.settings = settings
        self.ollama = ollama
        self.model_runtime = model_runtime
        self.formatter = formatter
        self.prompts = prompts

    def resolve_enhancement_model(
        self,
        *,
        llm_model: str | None,
        model_reference: ModelReference | None,
        agent_model_defaults: AgentModelDefaults | None,
    ) -> ResolvedModel:
        reference = model_reference
        agent_reference = agent_model_defaults.vision if agent_model_defaults else None
        settings_reference = self.settings.default_model_references.get("vision")
        if reference is None and llm_model:
            reference = ModelReference(model=llm_model)
        if reference is None and agent_reference is None and not settings_reference and self.settings.ocr_llm_model:
            reference = ModelReference(model=self.settings.ocr_llm_model)
        try:
            return self.model_runtime.resolve(
                "vision",
                model_reference=reference,
                agent_model_defaults=agent_model_defaults,
            )
        except ProviderRuntimeError as exc:
            raise DocumentProcessingError(str(exc)) from exc

    def resolve_quality_model(
        self,
        enhancement_model: ResolvedModel,
        triage_model: str | None,
    ) -> ResolvedModel:
        model = triage_model
        if model is None and enhancement_model.kind == "ollama":
            model = self.settings.ocr_llm_triage_model
        if not model:
            return enhancement_model
        try:
            return self.model_runtime.resolve(
                "vision",
                model_reference=ModelReference(
                    provider_profile_id=enhancement_model.profile_id,
                    model=model,
                ),
            )
        except ProviderRuntimeError as exc:
            raise DocumentProcessingError(str(exc)) from exc

    async def enhance_pages(
        self,
        pages: list[dict[str, Any]],
        model: str | ResolvedModel,
        *,
        triage_model: str | ResolvedModel | None = None,
        progress: ProgressCallback | None = None,
        force_repair: bool = False,
    ) -> None:
        """Triage OCR quality with a small model and only repair the poor pages.

        Pages rated ``good`` or ``average`` keep their OCR text untouched, which
        avoids a slow vision-model rewrite for the majority of a typical paper.
        """
        resolved_model = self._coerce_vision_model(model)
        quality_model = (
            triage_model
            if isinstance(triage_model, ResolvedModel)
            else self._coerce_vision_model(triage_model, profile_id=resolved_model.profile_id)
            if triage_model
            else resolved_model
        )
        if force_repair:
            for page in pages:
                page.setdefault("ocr_quality_issues", [])
            targets = list(pages)
        else:
            targets = await self._triage_pages(pages, quality_model, progress=progress)
        repaired = await self._repair_pages(targets, resolved_model, progress=progress)
        await self._validate_repaired_pages(repaired, quality_model, progress=progress)

    def _coerce_vision_model(
        self,
        model: str | ResolvedModel,
        *,
        profile_id: str | None = None,
    ) -> ResolvedModel:
        if isinstance(model, ResolvedModel):
            return model
        return self.model_runtime.resolve(
            "vision",
            model_reference=ModelReference(provider_profile_id=profile_id, model=model),
        )

    async def _generate_vision(
        self,
        model: ResolvedModel,
        prompt: str,
        *,
        format_: dict[str, Any] | str | None = None,
        image_png: bytes,
    ) -> dict[str, Any]:
        image = base64.b64encode(image_png).decode("ascii")
        ollama_base_url = getattr(self.ollama, "base_url", model.base_url)
        if (
            model.kind == "ollama"
            and self.ollama.profile_id == model.profile_id
            and str(ollama_base_url).rstrip("/") == model.base_url.rstrip("/")
        ):
            kwargs: dict[str, Any] = {
                "stream": False,
                "options": {"temperature": 0},
                "images": [image],
                "think": False,
            }
            if format_ is not None:
                kwargs["format_"] = format_
            request = {"prompt": prompt, **kwargs}
            try:
                response = await self.ollama.generate(model.model, prompt, **kwargs)
            except OllamaError as exc:
                self.model_runtime.llm_logger.write(
                    provider=model.kind, model=model.model, operation="ollama.generate", request=request, error=exc
                )
                raise
            self.model_runtime.llm_logger.write(
                provider=model.kind, model=model.model, operation="ollama.generate", request=request, response=response
            )
            return response
        return await self.model_runtime.generate(
            model,
            prompt,
            temperature=0,
            format_=format_,
            images=[image],
            think=False,
        )

    async def _triage_pages(
        self,
        pages: list[dict[str, Any]],
        model: ResolvedModel,
        *,
        progress: ProgressCallback | None = None,
    ) -> list[dict[str, Any]]:
        durations: list[float] = []
        total_pages = len(pages)
        poor_pages: list[dict[str, Any]] = []
        await report_progress(
            progress,
            {
                "phase": "ocr_triage",
                "phase_label": "Checking OCR quality",
                "completed_pages": 0,
                "total_pages": total_pages,
            },
        )
        for completed, page in enumerate(pages, start=1):
            page_started_at = time.monotonic()
            image_png = page.get("_image_png")
            if not isinstance(image_png, bytes):
                raise DocumentProcessingError(
                    f"Rendered image is missing for OCR triage of page {page['page']}"
                )
            ocr_text = str(page.get("raw_text") or "").strip()
            if not ocr_text:
                quality = "poor"
                issues = ["OCR produced no text for this page."]
            else:
                prompt = (
                    (
                        self.prompts.render("ocr-triage")
                        if self.prompts is not None
                        else (
                            "Grade how faithfully OCR captured one PDF page. Do not rewrite it. "
                            "Return only the requested quality JSON."
                        )
                    )
                    + f"\n\nOCR TEXT:\n{ocr_text}"
                )
                try:
                    response = await self._generate_vision(
                        model,
                        prompt,
                        format_=OCR_QUALITY_SCHEMA,
                        image_png=image_png,
                    )
                except (OllamaError, OpenAIError, ProviderRuntimeError) as exc:
                    quality = "poor"
                    issues = [f"OCR quality check failed: {exc}"]
                else:
                    assessment, parse_error = self._parse_quality_response(response)
                    if parse_error:
                        quality = "poor"
                        issues = [parse_error]
                    else:
                        quality = assessment["quality"]
                        issues = assessment["issues"]
            page["ocr_quality"] = quality
            page["ocr_quality_issues"] = issues
            if quality == "poor":
                poor_pages.append(page)
            else:
                self._keep_ocr_text(page)
            durations.append(time.monotonic() - page_started_at)
            average = sum(durations) / len(durations)
            await report_progress(
                progress,
                {
                    "phase": "ocr_triage",
                    "phase_label": "Checking OCR quality",
                    "completed_pages": completed,
                    "total_pages": total_pages,
                    "current_page": page["page"],
                    "page_quality": quality,
                    "pages_needing_repair": len(poor_pages),
                    "page_seconds": round(durations[-1], 1),
                    "average_seconds_per_page": round(average, 1),
                    "eta_seconds": round(average * (total_pages - completed), 1),
                },
            )
        return poor_pages

    async def _repair_pages(
        self,
        pages: list[dict[str, Any]],
        model: ResolvedModel,
        *,
        progress: ProgressCallback | None = None,
    ) -> list[dict[str, Any]]:
        if not pages:
            return []
        durations: list[float] = []
        total_pages = len(pages)
        repaired: list[dict[str, Any]] = []
        await report_progress(
            progress,
            {
                "phase": "llm_enhancement",
                "phase_label": "Rewriting low-quality OCR pages",
                "completed_pages": 0,
                "total_pages": total_pages,
            },
        )
        for completed, page in enumerate(pages, start=1):
            page_started_at = time.monotonic()
            image_png = page.get("_image_png")
            if not isinstance(image_png, bytes):
                raise DocumentProcessingError(f"Rendered image is missing for page {page['page']}")
            prompt = (
                (
                    self.prompts.render("ocr-reconstruction")
                    if self.prompts is not None
                    else (
                        "Transcribe and reconstruct this single PDF page as faithful Markdown using "
                        "the page image as the source of truth and the OCR text as a draft. Correct "
                        "OCR errors only when the image supports the correction. Preserve every "
                        "heading, paragraph, list, equation, footnote, and table; use GitHub-flavored "
                        "Markdown tables when appropriate. Do not summarize, explain, or invent "
                        "content. Return only the page Markdown without analysis or a code fence."
                    )
                )
                + f"\n\nOCR TEXT:\n{page.get('raw_text') or '(empty)'}"
            )
            quality_issues = page.get("ocr_quality_issues")
            if quality_issues:
                joined = "\n".join(f"- {issue}" for issue in quality_issues)
                prompt += (
                    "\n\nA quality check flagged the following problems with the OCR draft. "
                    f"Pay particular attention to them:\n{joined}"
                )
            try:
                response = await self._generate_vision(
                    model,
                    prompt,
                    image_png=image_png,
                )
            except (OllamaError, OpenAIError, ProviderRuntimeError) as exc:
                self._use_ocr_fallback(page, f"the LLM request failed: {exc}")
            else:
                enhanced = self._strip_markdown_fence(self._ollama_response_text(response))
                if not enhanced:
                    detail = (
                        "the model returned only an internal thinking trace"
                        if response.get("thinking")
                        else "the model returned no page content"
                    )
                    self._use_ocr_fallback(page, detail)
                else:
                    page["text"] = enhanced
                    page["llm_enhanced"] = True
                    page["llm_validation_status"] = "pending"
                    page.pop("llm_enhancement_note", None)
                    repaired.append(page)
            durations.append(time.monotonic() - page_started_at)
            average = sum(durations) / len(durations)
            await report_progress(
                progress,
                {
                    "phase": "llm_enhancement",
                    "phase_label": "Rewriting low-quality OCR pages",
                    "completed_pages": completed,
                    "total_pages": total_pages,
                    "current_page": page["page"],
                    "page_seconds": round(durations[-1], 1),
                    "average_seconds_per_page": round(average, 1),
                    "eta_seconds": round(average * (total_pages - completed), 1),
                },
            )
        return repaired

    async def _validate_repaired_pages(
        self,
        pages: list[dict[str, Any]],
        model: ResolvedModel,
        *,
        progress: ProgressCallback | None = None,
    ) -> None:
        if not pages:
            return
        durations: list[float] = []
        total_pages = len(pages)
        await report_progress(
            progress,
            {
                "phase": "llm_validation",
                "phase_label": "Validating rewritten pages",
                "completed_pages": 0,
                "total_pages": total_pages,
            },
        )
        for completed, page in enumerate(pages, start=1):
            page_started_at = time.monotonic()
            image_png = page.get("_image_png")
            if not isinstance(image_png, bytes):
                raise DocumentProcessingError(
                    f"Rendered image is missing for validation of page {page['page']}"
                )
            prompt = (
                (
                    self.prompts.render("ocr-validation")
                    if self.prompts is not None
                    else (
                        "Grade this Markdown reconstruction against the page image and OCR draft. "
                        "Return only the requested quality JSON."
                    )
                )
                + f"\n\nOCR DRAFT:\n{page.get('raw_text') or '(empty)'}\n\n"
                + f"CANDIDATE MARKDOWN:\n{page.get('text') or '(empty)'}"
            )
            try:
                response = await self._generate_vision(
                    model,
                    prompt,
                    format_=OCR_QUALITY_SCHEMA,
                    image_png=image_png,
                )
            except (OllamaError, OpenAIError, ProviderRuntimeError) as exc:
                self._reject_enhancement(page, [f"Validation request failed: {exc}"])
            else:
                assessment, parse_error = self._parse_quality_response(response)
                if parse_error:
                    self._reject_enhancement(page, [parse_error])
                elif assessment["quality"] == "poor":
                    issues = assessment["issues"] or [
                        "The validation model rated the rewritten page as poor without details."
                    ]
                    self._reject_enhancement(page, issues)
                else:
                    page["llm_validation_status"] = "passed"
                    page["llm_validation_quality"] = assessment["quality"]
                    page["llm_validation_issues"] = assessment["issues"]
            durations.append(time.monotonic() - page_started_at)
            average = sum(durations) / len(durations)
            await report_progress(
                progress,
                {
                    "phase": "llm_validation",
                    "phase_label": "Validating rewritten pages",
                    "completed_pages": completed,
                    "total_pages": total_pages,
                    "current_page": page["page"],
                    "page_seconds": round(durations[-1], 1),
                    "average_seconds_per_page": round(average, 1),
                    "eta_seconds": round(average * (total_pages - completed), 1),
                },
            )

    def _keep_ocr_text(self, page: dict[str, Any]) -> None:
        page["text"] = str(page.get("raw_text") or page.get("text") or "")
        page["llm_enhanced"] = False
        for key in ("llm_enhancement_note", "llm_validation_status", "llm_validation_issues", "llm_validation_quality"):
            page.pop(key, None)

    def _reject_enhancement(self, page: dict[str, Any], issues: list[str]) -> None:
        page["llm_validation_status"] = "failed"
        page["llm_validation_issues"] = issues
        self._use_ocr_fallback(
            page,
            f"the validation check rejected it: {'; '.join(issues)}",
        )

    def _parse_quality_response(
        self,
        response: dict[str, Any],
    ) -> tuple[dict[str, Any], str | None]:
        raw = self._ollama_response_text(response)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            return {}, f"The quality model returned invalid JSON: {exc.msg}"
        if not isinstance(payload, dict):
            return {}, "The quality model returned a non-object result."
        quality = payload.get("quality")
        issues = payload.get("issues", [])
        if isinstance(quality, str):
            quality = quality.strip().lower()
        if quality not in OCR_QUALITY_LEVELS:
            return {}, "The quality model did not return a good/average/poor rating."
        if not isinstance(issues, list) or not all(isinstance(issue, str) for issue in issues):
            return {}, "The quality model returned an invalid issues list."
        return {"quality": quality, "issues": issues}, None

    def _use_ocr_fallback(self, page: dict[str, Any], reason: str) -> None:
        page["text"] = str(page.get("raw_text") or page.get("text") or "")
        page["llm_enhanced"] = False
        page["llm_enhancement_note"] = (
            f"No LLM enhancement was applied because {reason}. "
            "The original OCR output is used directly."
        )

    @staticmethod
    def _ollama_response_text(response: dict[str, Any]) -> str:
        content = response.get("response")
        if not content and isinstance(response.get("message"), dict):
            content = response["message"].get("content")
        return str(content or "").strip()

    @staticmethod
    def _strip_markdown_fence(text: str) -> str:
        match = re.fullmatch(r"```(?:markdown|md)?\s*\n(.*?)\n```", text, flags=re.DOTALL | re.IGNORECASE)
        return match.group(1).strip() if match else text
