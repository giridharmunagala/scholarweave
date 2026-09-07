from __future__ import annotations

import argparse
import json
from pathlib import Path

from backend.core.config import Settings
from backend.workspace.upgrade import WorkspaceUpgrade


def main() -> None:
    parser = argparse.ArgumentParser(description="Preview or apply the offline research-workspace layout upgrade.")
    operation = parser.add_mutually_exclusive_group()
    operation.add_argument("--apply", action="store_true", help="Back up research data, relocate files and retire old conversations.")
    operation.add_argument("--restore", action="store_true", help="Restore the matched original files and database backup.")
    parser.add_argument("--offline", action="store_true", help="Confirm ScholarWeave and other workspace writers are stopped.")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--workspace-dir", type=Path)
    arguments = parser.parse_args()
    if (arguments.apply or arguments.restore) and not arguments.offline:
        parser.error("Stop ScholarWeave and all workspace writers, then pass --offline.")
    settings = Settings(**{name: getattr(arguments, name) for name in ("data_dir", "workspace_dir")
                          if getattr(arguments, name) is not None})
    upgrade = WorkspaceUpgrade(settings)
    if arguments.restore:
        upgrade.restore()
        print("Restored original research files and database. Pre-restore directories were retained.")
    else:
        result = upgrade.apply() if arguments.apply else upgrade.preview()
        print(json.dumps({key: value for key, value in result.items() if key not in {"entries", "legacy_tags"}}, indent=2))


if __name__ == "__main__":
    main()