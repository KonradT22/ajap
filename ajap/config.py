from __future__ import annotations

import json
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").split("#")[0].strip()  # strip accidental inline comments
GEMINI_RPM: int = int(os.getenv("GEMINI_RPM", "150"))  # paid Tier-1 ≈150-300 RPM; free tier ≈10
DISCORD_WEBHOOK_URL: str = os.getenv("DISCORD_WEBHOOK_URL", "")
SMTP_HOST: str = os.getenv("SMTP_HOST", "")
SMTP_PORT: str = os.getenv("SMTP_PORT", "587")
SMTP_USER: str = os.getenv("SMTP_USER", "")
SMTP_PASS: str = os.getenv("SMTP_PASS", "")
SMTP_FROM: str = os.getenv("SMTP_FROM", "")
SMTP_TO: str = os.getenv("SMTP_TO", "")
GOOGLE_CREDENTIALS_PATH: str = os.getenv("GOOGLE_CREDENTIALS_PATH", "credentials.json")
GOOGLE_SHEET_ID: str = os.getenv("GOOGLE_SHEET_ID", "")
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
