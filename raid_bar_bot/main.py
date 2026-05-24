"""
Raid Bar → G2G publishing bot.

Usage:
  python main.py               # run continuously (every check_interval_minutes)
  python main.py --once        # single cycle then exit
  python main.py --parse-only  # fetch and print accounts without posting
  python main.py --debug       # verbose logging
"""

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

import telebot

from formatter import format_description, format_title
from g2g_poster import G2GPoster
from parser import AccountData, fetch_account_list, fetch_accounts

PUBLISHED_FILE = Path("published_ids.json")
CONFIG_FILE = Path("config.json")


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _setup_logging(debug: bool) -> None:
    level = logging.DEBUG if debug else logging.INFO
    fmt = "%(asctime)s [%(levelname)s] %(name)s — %(message)s"
    handlers: list[logging.Handler] = [
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ]
    logging.basicConfig(level=level, format=fmt, handlers=handlers)


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------

def load_config() -> dict:
    with open(CONFIG_FILE, encoding="utf-8") as f:
        return json.load(f)


def load_published() -> dict[str, str]:
    if not PUBLISHED_FILE.exists():
        return {}
    with open(PUBLISHED_FILE, encoding="utf-8") as f:
        return json.load(f)


def save_published(data: dict[str, str]) -> None:
    with open(PUBLISHED_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def tg(bot: telebot.TeleBot, chat_id: str, text: str) -> None:
    try:
        bot.send_message(chat_id, text, parse_mode="HTML")
    except Exception as exc:
        logging.getLogger(__name__).warning(f"Telegram send failed: {exc}")


# ---------------------------------------------------------------------------
# Main cycle
# ---------------------------------------------------------------------------

logger = logging.getLogger(__name__)


async def run_cycle(config: dict, poster: G2GPoster, bot: telebot.TeleBot) -> None:
    chat_id = config["telegram_chat_id"]

    logger.info("=== Scanning raid.bar/pr ===")

    # Step 1: get the lightweight ID+price list (no detail pages yet)
    try:
        id_list = await fetch_account_list()
    except Exception as exc:
        logger.error(f"Failed to fetch account list: {exc}")
        return

    current_ids = {aid for aid, _, _ in id_list}
    prices = {aid: price for aid, _, price in id_list}
    urls = {aid: url for aid, url, _ in id_list}

    published: dict[str, str] = load_published()
    published_ids = set(published.keys())

    new_ids = current_ids - published_ids
    removed_ids = published_ids - current_ids

    logger.info(
        f"Live: {len(current_ids)}  |  New: {len(new_ids)}  |  Removed: {len(removed_ids)}"
    )

    # Step 2: fetch full details only for new accounts
    if new_ids:
        from parser import fetch_single_account

        for account_id in list(new_ids):
            detail_url = urls[account_id]
            price = prices[account_id]
            logger.info(f"Publishing {account_id}  (${price:.2f}) …")

            try:
                acc = await fetch_single_account(account_id, detail_url, price)
                if acc is None:
                    logger.error(f"Could not fetch details for {account_id} — skipping")
                    continue

                title = format_title(acc)
                description = format_description(acc)

                lot_id = await poster.create_listing(title, description, price)
                if lot_id:
                    published[account_id] = lot_id
                    save_published(published)
                    logger.info(f"✓ {account_id} → lot {lot_id}")
                    tg(
                        bot, chat_id,
                        f"✅ <b>Published</b>\n"
                        f"🔑 ID: <code>{account_id}</code>\n"
                        f"💰 Price: <b>${price:.2f}</b>\n"
                        f"🏷 Lot: <code>{lot_id}</code>\n"
                        f"📝 {title[:120]}",
                    )
                else:
                    logger.error(f"✗ Failed to create listing for {account_id}")

            except Exception as exc:
                logger.error(f"Error publishing {account_id}: {exc}", exc_info=True)

    # Step 3: deactivate removed accounts
    for account_id in list(removed_ids):
        lot_id = published.get(account_id)
        if not lot_id:
            published.pop(account_id, None)
            save_published(published)
            continue

        logger.info(f"Deactivating lot {lot_id} (account {account_id} gone) …")
        try:
            ok = await poster.deactivate_listing(lot_id)
            if ok:
                published.pop(account_id)
                save_published(published)
                logger.info(f"✓ Deactivated lot {lot_id}")
                tg(
                    bot, chat_id,
                    f"❌ <b>Deactivated</b>\n"
                    f"🔑 ID: <code>{account_id}</code>\n"
                    f"🏷 Lot: <code>{lot_id}</code>",
                )
            else:
                logger.error(f"✗ Could not deactivate lot {lot_id}")
        except Exception as exc:
            logger.error(f"Error deactivating {lot_id}: {exc}", exc_info=True)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    ap = argparse.ArgumentParser(description="Raid Bar → G2G bot")
    ap.add_argument("--once", action="store_true", help="Run one cycle and exit")
    ap.add_argument("--parse-only", action="store_true", help="Print accounts without posting")
    ap.add_argument("--debug", action="store_true", help="Verbose logging")
    args = ap.parse_args()

    _setup_logging(args.debug)

    config = load_config()

    # Ensure state file exists
    if not PUBLISHED_FILE.exists():
        save_published({})

    # --parse-only: just print what's on the site
    if args.parse_only:
        accounts = await fetch_accounts()
        for acc in accounts:
            print(f"\n{'─'*60}")
            print(f"ID:     {acc.account_id}")
            print(f"Price:  ${acc.price_usd:.2f}")
            print(f"Mythic ({len(acc.mythic_champions)}): {', '.join(acc.mythic_champions)}")
            print(f"Legs   ({len(acc.legendary_champions)}): {', '.join(acc.legendary_champions[:6])}…")
            print(f"TITLE: {format_title(acc)}")
        print(f"\nTotal: {len(accounts)} accounts")
        return

    bot = telebot.TeleBot(config["telegram_token"])

    async with G2GPoster(config) as poster:
        logger.info("Bot ready.")
        tg(
            bot, config["telegram_chat_id"],
            f"🤖 <b>Raid Bar Bot started</b>\n"
            f"Scanning every {config.get('check_interval_minutes', 10)} min",
        )

        if args.once:
            await run_cycle(config, poster, bot)
            return

        while True:
            await run_cycle(config, poster, bot)
            interval = config.get("check_interval_minutes", 10) * 60
            logger.info(f"Next scan in {interval // 60} min …")
            await asyncio.sleep(interval)


if __name__ == "__main__":
    asyncio.run(main())
