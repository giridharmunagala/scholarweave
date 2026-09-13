"""Spawned backend entry point; no application imports in the terminal process."""

from __future__ import annotations

import multiprocessing
import os
import socket
import sys
import threading
from multiprocessing.synchronize import Event
from pathlib import Path


def serve(sock: socket.socket, stop: Event, root: Path, log_path: Path) -> None:
    os.chdir(root)
    with log_path.open("a", encoding="utf-8", buffering=1) as log:
        sys.stdout = sys.stderr = log
        import uvicorn

        server = uvicorn.Server(uvicorn.Config(
            "backend.app:app",
            host="127.0.0.1",
            port=sock.getsockname()[1],
            workers=1,
            timeout_graceful_shutdown=10,
            access_log=False,
        ))
        parent = multiprocessing.parent_process()

        def watch_parent() -> None:
            while not stop.wait(0.25):
                if parent is not None and not parent.is_alive():
                    break
            server.should_exit = True

        threading.Thread(target=watch_parent, name="launcher-lifetime", daemon=True).start()
        try:
            server.run(sockets=[sock])
        finally:
            sock.close()
