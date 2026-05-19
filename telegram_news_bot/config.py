"""
Bot configuration — loaded from environment variables or .env file.
"""
import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    # Telegram
    bot_token: str = field(default_factory=lambda: os.getenv("BOT_TOKEN", ""))
    channel_id: str = field(default_factory=lambda: os.getenv("CHANNEL_ID", ""))

    # Parser
    check_interval_seconds: int = int(os.getenv("CHECK_INTERVAL", "300"))  # 5 min
    max_articles_per_run: int = int(os.getenv("MAX_ARTICLES_PER_RUN", "5"))

    # Storage
    db_path: str = os.getenv("DB_PATH", "published.json")

    # HTTP
    request_timeout: int = int(os.getenv("REQUEST_TIMEOUT", "15"))
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )

    def validate(self) -> None:
        if not self.bot_token:
            raise ValueError("BOT_TOKEN is required. Set it in .env or environment.")
        if not self.channel_id:
            raise ValueError("CHANNEL_ID is required. Set it in .env or environment.")


config = Config()
