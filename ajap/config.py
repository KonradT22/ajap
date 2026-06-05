from __future__ import annotations

import json
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")  # 2.0-flash retired Mar 2026
DISCORD_WEBHOOK_URL: str = os.getenv("DISCORD_WEBHOOK_URL", "")
DB_PATH: str = os.getenv("DB_PATH", "data/ajap.db")
POLL_INTERVAL_MINUTES: int = int(os.getenv("POLL_INTERVAL_MINUTES", "30"))

_PROFILE_PATH = Path("config/profile.json")


def load_profile() -> dict:
    if not _PROFILE_PATH.exists():
        raise FileNotFoundError(
            f"Profile config not found at {_PROFILE_PATH}. "
            "Copy config/profile.example.json → config/profile.json and fill in your data."
        )
    with _PROFILE_PATH.open() as fh:
        return json.load(fh)
