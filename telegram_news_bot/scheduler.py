"""
Polling scheduler — runs fetch → filter → publish cycle every N seconds.
Uses a simple time-based loop (no external scheduler dependency).
"""
import logging
import time
from typing import Optional

import telebot

from .config import Config
from .parser import GohаParser
from .publisher import publish_article
from .storage import PublishedStorage

logger = logging.getLogger(__name__)


class NewsScheduler:
    def __init__(self, config: Config) -> None:
        config.validate()
        self._config = config
        self._bot = telebot.TeleBot(config.bot_token, parse_mode=None)
        self._storage = PublishedStorage(config.db_path)
        self._parser = GohаParser(config.user_agent, config.request_timeout)

    def run_once(self) -> int:
        """Fetch + publish one cycle. Returns number of articles published."""
        logger.info("--- Checking goha.ru for new articles ---")
        articles = self._parser.fetch_articles()

        new_articles = [a for a in articles if not self._storage.is_published(a.uid)]
        if not new_articles:
            logger.info("No new articles.")
            return 0

        # Publish at most N per run to avoid flooding
        to_publish = new_articles[: self._config.max_articles_per_run]
        published = 0
        for article in to_publish:
            if publish_article(self._bot, self._config.channel_id, article):
                self._storage.mark_published(article.uid)
                published += 1
                # Small delay between posts to respect Telegram rate limits
                time.sleep(2)

        logger.info("Published %d new article(s) this cycle.", published)
        return published

    def run_forever(self) -> None:
        """Blocking main loop."""
        logger.info(
            "Bot started. Checking every %d seconds. Channel: %s",
            self._config.check_interval_seconds,
            self._config.channel_id,
        )
        while True:
            try:
                self.run_once()
            except KeyboardInterrupt:
                logger.info("Interrupted. Stopping bot.")
                break
            except Exception as exc:
                # Log and continue — never crash the loop
                logger.exception("Unexpected error during cycle: %s", exc)

            time.sleep(self._config.check_interval_seconds)
