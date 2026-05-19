"""
Entry point.

Usage:
    python -m telegram_news_bot.main          # run forever
    python -m telegram_news_bot.main --once   # single check (useful for cron)
"""
import argparse
import logging
import sys

from .config import config
from .scheduler import NewsScheduler


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("bot.log", encoding="utf-8"),
        ],
    )
    # Silence noisy third-party loggers
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("telebot").setLevel(logging.WARNING)


def main() -> None:
    setup_logging()

    parser = argparse.ArgumentParser(description="goha.ru → Telegram news bot")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single fetch cycle and exit (good for cron jobs)",
    )
    args = parser.parse_args()

    scheduler = NewsScheduler(config)

    if args.once:
        count = scheduler.run_once()
        print(f"Done. Published {count} article(s).")
    else:
        scheduler.run_forever()


if __name__ == "__main__":
    main()
