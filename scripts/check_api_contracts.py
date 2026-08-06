from __future__ import annotations

import difflib
from pathlib import Path
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="scholarweave-contracts-") as runtime:
        generated_openapi = Path(runtime) / "openapi.json"
        generated_types = Path(runtime) / "schema.generated.ts"
        subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "export_openapi.py"), str(generated_openapi)],
            cwd=ROOT,
            check=True,
        )
        subprocess.run(
            [
                str(FRONTEND / "node_modules" / ".bin" / "openapi-typescript"),
                str(generated_openapi),
                "-o",
                str(generated_types),
            ],
            cwd=FRONTEND,
            check=True,
        )
        stale = False
        stale |= _report_difference(FRONTEND / "openapi.json", generated_openapi)
        stale |= _report_difference(
            FRONTEND / "src" / "api" / "schema.generated.ts",
            generated_types,
        )
        if stale:
            raise SystemExit(
                "Generated API contracts are stale. Run `cd frontend && npm run generate:api`."
            )


def _report_difference(expected_path: Path, actual_path: Path) -> bool:
    expected = expected_path.read_text(encoding="utf-8")
    actual = actual_path.read_text(encoding="utf-8")
    if expected == actual:
        return False
    difference = difflib.unified_diff(
        expected.splitlines(),
        actual.splitlines(),
        fromfile=str(expected_path),
        tofile=f"generated:{expected_path.name}",
        lineterm="",
        n=3,
    )
    print("\n".join(list(difference)[:80]), file=sys.stderr)
    return True


if __name__ == "__main__":
    main()
