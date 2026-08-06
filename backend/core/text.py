from __future__ import annotations

import re


def clean_filename(filename: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", filename).strip("._")
    return cleaned or "file"
