#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

VENV_PYTHON=".venv/bin/python"

if [ ! -x "$VENV_PYTHON" ]; then
    echo "Creating virtual environment…"
    python3 -m venv .venv
    .venv/bin/pip install --quiet -r requirements.txt
fi

# Check if first argument is "web"
if [ "${1:-}" = "web" ]; then
    echo "Starting web interface on http://localhost:5000"
    exec "$VENV_PYTHON" web_app.py
else
    exec "$VENV_PYTHON" worker.py "$@"
fi
