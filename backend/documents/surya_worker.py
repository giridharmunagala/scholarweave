from __future__ import annotations

import argparse
import gc
import json
import re
import sys
from pathlib import Path
from typing import Any

SURYA_OCR_PROMPT = (
    "OCR this image to HTML. Each block is a div with data-label and data-bbox "
    "(x0 y0 x1 y1, normalized 0-1000)."
)


def _device_name(requested: str, torch: Any) -> str:
    if requested == "auto":
        return "cuda" if torch.cuda.is_available() else "cpu"
    if requested == "cuda" and not torch.cuda.is_available():
        raise RuntimeError("Surya is configured for CUDA, but CUDA is unavailable.")
    return requested


def _prepare_image(path: Path, max_width: int) -> Any:
    from PIL import Image

    image = Image.open(path).convert("RGB")
    if image.width > max_width:
        height = max(1, round(image.height * max_width / image.width))
        image = image.resize((max_width, height), Image.Resampling.LANCZOS)
    return image


def _strip_fences(value: str) -> str:
    cleaned = value.strip()
    cleaned = re.sub(r"^```(?:html)?\s*", "", cleaned, flags=re.IGNORECASE)
    return re.sub(r"\s*```$", "", cleaned).strip()


def _html_to_markdown(value: str) -> str:
    from markdownify import markdownify

    return markdownify(_strip_fences(value), heading_style="ATX").strip()


def run(manifest_path: Path, output_path: Path) -> None:
    import torch
    from transformers import AutoModelForImageTextToText, AutoProcessor

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    model_name = str(manifest["model"])
    cache_dir = str(manifest["cache_dir"])
    device = _device_name(str(manifest["device"]), torch)
    image_paths = [Path(value) for value in manifest["images"]]
    max_new_tokens = int(manifest["max_new_tokens"])
    max_image_width = int(manifest["max_image_width"])

    dtype = torch.bfloat16 if device == "cuda" else torch.float32
    processor = None
    model = None
    try:
        processor = AutoProcessor.from_pretrained(model_name, cache_dir=cache_dir)
        model = AutoModelForImageTextToText.from_pretrained(
            model_name,
            cache_dir=cache_dir,
            dtype=dtype,
            low_cpu_mem_usage=True,
        )
        model.to(device)
        model.eval()

        pages: list[str] = []
        for image_path in image_paths:
            image = _prepare_image(image_path, max_image_width)
            messages = [
                {
                    "role": "user",
                    "content": [
                        {"type": "image", "image": image},
                        {"type": "text", "text": SURYA_OCR_PROMPT},
                    ],
                }
            ]
            inputs = processor.apply_chat_template(
                messages,
                add_generation_prompt=True,
                tokenize=True,
                return_dict=True,
                return_tensors="pt",
            ).to(device)
            with torch.inference_mode():
                generated = model.generate(
                    **inputs,
                    max_new_tokens=max_new_tokens,
                    do_sample=False,
                    use_cache=True,
                )
            prompt_length = inputs["input_ids"].shape[1]
            raw_html = processor.batch_decode(
                generated[:, prompt_length:],
                skip_special_tokens=True,
                clean_up_tokenization_spaces=False,
            )[0]
            pages.append(_html_to_markdown(raw_html))
            image.close()
            del inputs, generated
            if device == "cuda":
                torch.cuda.empty_cache()

        output_path.write_text(
            json.dumps({"pages": pages, "device": device}, ensure_ascii=False),
            encoding="utf-8",
        )
    finally:
        del model, processor
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            torch.cuda.ipc_collect()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        run(args.manifest, args.output)
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
