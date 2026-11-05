"""Single place that loads secrets/config from apps/youtube-download/.env.

Import this module (``import settings``) before reading any of:
ANTHROPIC_API_KEY, MFO_WP_*, NCBI_*, REDDIT_*, NTFY_TOPIC, ...

Rules:
- Never overrides a variable that is already set in the real environment.
- Skipped under pytest (or with MFO_SKIP_DOTENV=1) so tests can never pick
  up the live API key and accidentally make paid calls.
- Never prints or logs any value.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = APP_DIR / ".env"


def load() -> bool:
    if os.environ.get("MFO_SKIP_DOTENV") or "pytest" in sys.modules:
        return False
    if not ENV_PATH.exists():
        return False
    from dotenv import load_dotenv

    return load_dotenv(ENV_PATH, override=False)


LOADED = load()
