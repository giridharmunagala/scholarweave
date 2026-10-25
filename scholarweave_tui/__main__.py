from __future__ import annotations

import argparse
import importlib.util
import multiprocessing
import os
import time
import webbrowser
from pathlib import Path


def _positive_timeout(value: str) -> float:
    timeout = float(value)
    if not 1 <= timeout <= 600:
        raise argparse.ArgumentTypeError("Startup timeout must be between 1 and 600 seconds.")
    return timeout


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="scholarweave",
        description="ScholarWeave research cockpit. Starts or reuses your local backend automatically.",
    )
    parser.add_argument("--build-frontend", action="store_true", help="Build the web frontend before launching (requires Node.js).")
    parser.add_argument("--ensure-frontend", action="store_true", help="Build the web frontend only when its verified assets are missing or outdated.")
    parser.add_argument("--web", action="store_true", help="Open the web app in your browser instead of the terminal UI.")
    parser.add_argument("--connect-only", action="store_true", help="Use an existing backend without starting one.")
    parser.add_argument("--startup-timeout", type=_positive_timeout, default=60, help="Backend readiness timeout in seconds (default: 60).")
    parser.add_argument("--check", action="store_true", help="Check startup, then stop any owned backend without opening the TUI.")
    parser.add_argument(
        "--api-url",
        default="http://127.0.0.1:1102",
        help="Preferred local origin; automatically chooses a free port if occupied (default: http://127.0.0.1:1102). "
             "--connect-only requires this exact origin.",
    )
    args = parser.parse_args()
    if not args.web and importlib.util.find_spec("textual") is None:
        parser.exit(1, 'The terminal UI is optional. Install it with: pip install -e ".[tui]"\n')

    from scholarweave_tui.client import _local_origin
    from scholarweave_tui.launcher import BackendSession, LaunchError, build_frontend

    try:
        base_url = _local_origin(args.api_url)
    except ValueError as exc:
        parser.error(str(exc))
    root = Path(__file__).resolve().parents[1]
    os.chdir(root)
    try:
        if args.build_frontend or args.ensure_frontend:
            build_frontend(root, force=args.build_frontend)
        with BackendSession(
            base_url, root, connect_only=args.connect_only, timeout=args.startup_timeout,
        ) as session:
            base_url = session.base_url
            if args.web:
                session.check_web()
            if args.check:
                print("ScholarWeave startup check passed.", flush=True)
            elif args.web:
                if not webbrowser.open(base_url):
                    raise LaunchError(f"Could not open your browser. Open {base_url} manually.")
                if session.process is not None:
                    print(f"ScholarWeave is open at {base_url}. Keep this window open; Ctrl+C stops the server.", flush=True)
                    while session.process.is_alive():
                        time.sleep(0.5)
                    raise LaunchError(session._diagnostic("The web backend stopped unexpectedly."))
            else:
                from scholarweave_tui.app import ScholarWeaveApp
                from scholarweave_tui.client import ScholarWeaveClient

                ScholarWeaveApp(ScholarWeaveClient(base_url)).run()
    except LaunchError as exc:
        parser.exit(1, f"\nScholarWeave could not launch:\n{exc}\n")
    except KeyboardInterrupt:
        parser.exit(130, "\nScholarWeave startup cancelled.\n")


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
