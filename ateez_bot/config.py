"""Runtime configuration, read from environment variables (or a .env file)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    telegram_token: str
    anthropic_api_key: str
    allowed_user_id: int | None
    model: str
    db_path: str
    tz: ZoneInfo
    currency: str
    late_days: int
    daily_check_hour: int
    healthcheck_url: str | None


def load_config() -> Config:
    load_dotenv()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not token or not api_key:
        raise SystemExit(
            "Missing TELEGRAM_BOT_TOKEN or ANTHROPIC_API_KEY. Copy .env.example to .env and fill it in."
        )
    uid = os.environ.get("ALLOWED_USER_ID", "").strip()
    return Config(
        telegram_token=token,
        anthropic_api_key=api_key,
        allowed_user_id=int(uid) if uid else None,
        model=os.environ.get("CLAUDE_MODEL", "claude-haiku-5-5"),
        db_path=os.environ.get("DB_PATH", "data/ateez.db"),
        tz=ZoneInfo(os.environ.get("TIMEZONE", "Asia/Singapore")),
        currency=os.environ.get("CURRENCY", "S$"),
        late_days=int(os.environ.get("LATE_DAYS", "28")),
        daily_check_hour=int(os.environ.get("DAILY_CHECK_HOUR", "10")),
        healthcheck_url=os.environ.get("HEALTHCHECK_URL", "").strip() or None,
    )
