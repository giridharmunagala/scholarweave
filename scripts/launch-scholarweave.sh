#!/bin/sh
set -eu

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
cd -- "$root"
python="$root/.venv/bin/python"

if [ ! -x "$python" ]; then
    printf '%s\n' 'Create .venv with Python 3.12+ and install .[tui] first; see README.md.' >&2
    exit 1
fi
if ! "$python" -c "import textual, uvicorn"; then
    printf '%s\n' 'From the repository root, run: .venv/bin/python -m pip install -e ".[tui]"' >&2
    exit 1
fi

exec "$python" -m scholarweave_tui --build-frontend "$@"
