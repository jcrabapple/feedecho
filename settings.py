"""Central configuration — the single source of truth for environment-driven settings."""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

MODE = os.environ.get("FEEDCHO_MODE", "single")
if MODE not in ("single", "multi"):
    raise ValueError(f"FEEDCHO_MODE must be 'single' or 'multi', got {MODE!r}")
MULTI = MODE == "multi"

DB_PATH = Path(os.environ.get("FEEDCHO_DB_PATH", BASE_DIR / "feedecho.db"))
DATABASE_URL = os.environ.get("FEEDCHO_DATABASE_URL", "")
AUTH_TOKEN = os.environ.get("FEEDCHO_AUTH_TOKEN", "")
CALLBACK_URL = os.environ.get("FEEDCHO_CALLBACK_URL", "")
STATE_SECRET = os.environ.get("FEEDCHO_STATE_SECRET", "")
BASE_URL = os.environ.get("FEEDCHO_BASE_URL", "")
SESSION_SECRET = os.environ.get("FEEDCHO_SESSION_SECRET", "")
