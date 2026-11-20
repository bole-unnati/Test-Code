"""
main.py — SDLC Automation Platform OSS Edition

Run from anywhere:
    python main.py
    python backend/main.py
    python backend\\main.py   (Windows)
"""
import os
import sys

# ── Fix sys.path FIRST — before any project imports ──────────────────────────
# On Windows, Python does not automatically add the script's folder to sys.path
# when the script is run from a parent directory. This means `from src.xxx`
# and `from config.xxx` fail with ModuleNotFoundError.
#
# Fix: add backend/ (the folder this file lives in) to sys.path explicitly.
# All other files (base.py, workflow.py, etc.) inherit this once main.py runs.

_BACKEND = os.path.dirname(os.path.abspath(__file__))  # .../backend/
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

# ── Now safe to import project modules ────────────────────────────────────────
import uvicorn
from config.settings import settings


def main():
    settings.print_status()

    uvicorn.run(
        "src.api.server:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=True,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
