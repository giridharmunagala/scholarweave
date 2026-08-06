from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

def main() -> None:
    parser = argparse.ArgumentParser(description="Export ScholarWeave's OpenAPI schema.")
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="scholarweave-openapi-") as runtime:
        root = Path(runtime)
        overrides = {
            "SCHOLARWEAVE_DATA_DIR": str(root / "data"),
            "SCHOLARWEAVE_WORKSPACE_DIR": str(root / "workspace"),
            "SCHOLARWEAVE_FRONTEND_DIST_DIR": str(root / "frontend"),
        }
        previous = {key: os.environ.get(key) for key in overrides}
        os.environ.update(overrides)
        try:
            from backend.app import app

            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(
                json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            asyncio.run(app.state.services.close())
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


if __name__ == "__main__":
    main()
