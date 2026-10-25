from __future__ import annotations

import base64
import io
import json
import logging
import math
import re
from typing import Any

import anyio
import httpx
from bs4 import BeautifulSoup, Tag
from markdownify import MarkdownConverter
from openai import APIError, AsyncOpenAI, NOT_GIVEN, OpenAI
from PIL import Image

from backend.core.config import Settings
from backend.core.errors import DocumentProcessingError

_logger = logging.getLogger(__name__)
# These are Surya 2's training-time prompts, not general chat instructions.
PAGE_PROMPT = (
    "OCR this image to HTML. Each block is a div with data-label and data-bbox "
    "(x0 y0 x1 y1, normalized 0-1000)."
)
LAYOUT_PROMPT = (
    "Output the layout of this image as JSON. Each entry is a dict with "
    '"label", "bbox", and "count" fields. Bbox is x0 y0 x1 y1, normalized 0-1000.'
)
BLOCK_PROMPT = "OCR this block image to HTML."
_LAYOUT_SCHEMA = {
    "type": "array", "maxItems": 200,
    "items": {
        "type": "object",
        "properties": {
            "label": {"type": "string"},
            "bbox": {"type": "string", "pattern": r"^\d{1,4} \d{1,4} \d{1,4} \d{1,4}$"},
            "count": {"type": "integer", "minimum": 0, "maximum": 10000},
        },
        "required": ["label", "bbox", "count"], "additionalProperties": False,
    },
}
_VISUAL_LABELS = {"image", "picture", "figure", "diagram"}
_MARGIN_LABELS = {"pageheader", "pagefooter", "blankpage"}
_SAFE_TAGS = {
    "p", "div", "span", "br", "b", "strong", "i", "em", "u", "del", "sup", "sub",
    "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li", "pre", "code",
    "table", "thead", "tbody", "tfoot", "tr", "td", "th", "caption", "math", "hr",
}


def _label(value: str) -> str:
    return re.sub(r"[\s_-]", "", value).lower()


def _bbox(value: Any) -> list[float]:
    parts = value.replace(",", " ").split() if isinstance(value, str) else value
    if not isinstance(parts, (list, tuple)) or len(parts) != 4:
        raise DocumentProcessingError("Surya returned a malformed block bounding box.")
    try:
        box = [float(part) for part in parts]
    except (TypeError, ValueError) as exc:
        raise DocumentProcessingError("Surya returned a nonnumeric bounding box.") from exc
    if not all(math.isfinite(n) and 0 <= n <= 1000 for n in box) or box[0] >= box[2] or box[1] >= box[3]:
        raise DocumentProcessingError("Surya returned an out-of-range or empty bounding box.")
    return box


def _strip_fence(value: str) -> str:
    return re.sub(r"\n```\s*$", "", re.sub(r"^```(?:html|json)?\s*\n", "", value.strip()))


class _Markdown(MarkdownConverter):
    def convert_sup(self, el: Tag, text: str, parent_tags: set[str]) -> str:
        return f"<sup>{text}</sup>"

    def convert_sub(self, el: Tag, text: str, parent_tags: set[str]) -> str:
        return f"<sub>{text}</sub>"

    def convert_math(self, el: Tag, text: str, parent_tags: set[str]) -> str:
        latex = el.get_text().strip()
        if el.get("display") == "block":
            return f"\n\n$$\n{latex}\n$$\n\n"
        return f"${latex}$"

    def convert_table(self, el: Tag, text: str, parent_tags: set[str]) -> str:
        # GFM has no spanning cells; retain sanitized HTML rather than lose geometry.
        if el.find(attrs={"rowspan": True}) or el.find(attrs={"colspan": True}):
            for math_tag in el.find_all("math"):
                math_tag.name = "span"
                math_tag.attrs = {"class": ["math", "math-inline"]}
            return f"\n\n{el}\n\n"
        return super().convert_table(el, text, parent_tags)


def block_markdown(html: str, label: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in list(soup.find_all(True)):
        if tag.name is None:
            continue
        if tag.name in {"script", "style", "iframe", "object", "img"}:
            tag.decompose()
        elif tag.name not in _SAFE_TAGS:
            tag.unwrap()
        else:
            tag.attrs = {
                key: value for key, value in tag.attrs.items()
                if (tag.name in {"td", "th"} and key in {"rowspan", "colspan"}
                    and str(value).isdigit() and 1 <= int(str(value)) <= 100)
                or (tag.name == "math" and key == "display" and value in {"block", "inline"})
            }
    if _label(label) in {"equation", "equationblock"}:
        for tag in soup.find_all("math"):
            tag["display"] = "block"
    text = _Markdown(heading_style="ATX", bullets="-").convert(str(soup)).strip()
    if _label(label) in {"sectionheader", "title"} and text and not text.startswith("#"):
        text = f"## {text}"
    return text


def parse_page(html: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(_strip_fence(html), "html.parser")
    if any(
        (isinstance(child, Tag) and child.name != "div")
        or (not isinstance(child, Tag) and str(child).strip())
        for child in soup.contents
    ):
        raise DocumentProcessingError("Surya returned content outside layout blocks.")
    blocks = []
    for div in soup.find_all("div", recursive=False):
        label = div.get("data-label")
        if not isinstance(label, str) or not label:
            raise DocumentProcessingError("Surya returned a block without a layout label.")
        blocks.append({
            "label": label,
            "bbox": _bbox(div.get("data-bbox")),
            "html": div.decode_contents(),
        })
    if not blocks:
        raise DocumentProcessingError("Surya returned no layout blocks.")
    return blocks


def _png(image: Image.Image) -> bytes:
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _crop(image: Image.Image, bbox: list[float]) -> Image.Image:
    x0, y0, x1, y1 = bbox
    return image.crop((
        math.floor(x0 * image.width / 1000), math.floor(y0 * image.height / 1000),
        math.ceil(x1 * image.width / 1000), math.ceil(y1 * image.height / 1000),
    ))


def _crop_png(image: Image.Image, bbox: list[float]) -> bytes:
    with _crop(image, bbox) as crop:
        return _png(crop)


def assemble_page(blocks: list[dict[str, Any]], image_png: bytes, page: int) -> dict[str, Any]:
    markdown = []
    figures = []
    with Image.open(io.BytesIO(image_png)) as image:
        for index, block in enumerate(blocks):
            block["reading_order"] = index
            label = _label(block["label"])
            block["markdown"] = block_markdown(block.pop("html"), block["label"])
            block["excluded_from_text"] = label in _MARGIN_LABELS
            if not block["markdown"] and label not in _VISUAL_LABELS | _MARGIN_LABELS:
                raise DocumentProcessingError("Surya returned an empty text block.")
            if label in _VISUAL_LABELS:
                path = f"figures/page-{page:04d}-{index + 1:03d}.png"
                caption = f"{block['label']} on page {page}"
                block["image_path"] = path
                block["markdown"] = f"![{caption}]({path})"
                figures.append({"path": path, "png": _crop_png(image, block["bbox"]), "block": index})
            if not block["excluded_from_text"] and block["markdown"]:
                markdown.append(block["markdown"])
        image_bbox = [0, 0, image.width, image.height]
    return {
        "text": "\n\n".join(markdown),
        "content_format": "markdown",
        "layout_blocks": blocks,
        "bbox_coordinate_system": "normalized_0_1000",
        "image_bbox": image_bbox,
        "_figures": figures,
    }


class SuryaOCR:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def available(self) -> bool:
        try:
            with OpenAI(
                base_url=self.settings.ocr_surya_base_url, api_key="local", timeout=5, max_retries=0,
                http_client=httpx.Client(trust_env=False, follow_redirects=False),
            ) as client:
                return any(model.id == self.settings.ocr_surya_model for model in client.models.list())
        except APIError as exc:
            _logger.warning("Surya OCR server is unavailable: %s", exc)
            return False

    async def _generate(self, client: AsyncOpenAI, image_png: bytes, prompt: str) -> str:
        try:
            response = await client.chat.completions.create(
                model=self.settings.ocr_surya_model,
                messages=[{"role": "user", "content": [
                    {"type": "image_url", "image_url": {
                        "url": "data:image/png;base64," + base64.b64encode(image_png).decode("ascii"),
                    }},
                    {"type": "text", "text": prompt},
                ]}],
                temperature=0,
                top_p=0.1,
                max_tokens=self.settings.ocr_surya_max_tokens,
                response_format={
                    "type": "json_schema",
                    "json_schema": {"name": "surya_layout", "strict": True, "schema": _LAYOUT_SCHEMA},
                } if prompt == LAYOUT_PROMPT else NOT_GIVEN,
            )
        except APIError as exc:
            raise DocumentProcessingError(f"Surya OCR request failed: {exc}") from exc
        if not response.choices:
            raise DocumentProcessingError("Surya OCR returned no completion.")
        choice = response.choices[0]
        if choice.finish_reason != "stop":
            raise DocumentProcessingError(
                f"Surya OCR did not finish ({choice.finish_reason}); check output/context limits."
            )
        text = choice.message.content or ""
        if not text.strip():
            raise DocumentProcessingError("Surya OCR returned empty content.")
        # Reject decoder loops even if the server reports a normal stop.
        tail = text.rstrip()
        for size in range(20, min(251, len(tail) // 6 + 1)):
            if tail.endswith(tail[-size:] * 6):
                raise DocumentProcessingError("Surya OCR produced repetitive output.")
        return text

    async def recognize(self, image_png: bytes, page: int) -> dict[str, Any]:
        async with AsyncOpenAI(
            base_url=self.settings.ocr_surya_base_url, api_key="local",
            timeout=self.settings.request_timeout_seconds, max_retries=1,
            http_client=httpx.AsyncClient(trust_env=False, follow_redirects=False),
        ) as client:
            try:
                html = await self._generate(client, image_png, PAGE_PROMPT)
                blocks = parse_page(html)
                result = await anyio.to_thread.run_sync(assemble_page, blocks, image_png, page)
                note = None
            except DocumentProcessingError as exc:
                note = f"Full-page OCR failed; recovered using layout and block OCR: {exc}"
                _logger.warning("Page %s: full-page OCR failed; trying layout and block OCR: %s", page, exc)
                layout = await self._generate(client, image_png, LAYOUT_PROMPT)
                try:
                    entries = json.loads(_strip_fence(layout))
                except json.JSONDecodeError as error:
                    raise DocumentProcessingError("Surya layout fallback returned invalid JSON.") from error
                if not isinstance(entries, list) or not 1 <= len(entries) <= 200:
                    raise DocumentProcessingError("Surya layout fallback returned an invalid block list.")
                blocks = []
                with Image.open(io.BytesIO(image_png)) as image:
                    for entry in entries:
                        if not isinstance(entry, dict) or not isinstance(entry.get("label"), str):
                            raise DocumentProcessingError("Surya layout fallback returned an invalid block.")
                        box = _bbox(entry.get("bbox"))
                        label = entry["label"]
                        html = ""
                        if _label(label) not in _VISUAL_LABELS | {"blankpage"}:
                            cropped_png = await anyio.to_thread.run_sync(_crop_png, image, box)
                            html = await self._generate(client, cropped_png, BLOCK_PROMPT)
                        blocks.append({"label": label, "bbox": box, "html": _strip_fence(html)})
                result = await anyio.to_thread.run_sync(assemble_page, blocks, image_png, page)
            if note:
                result["ocr_recovery_note"] = note
            return result
