"""
Raid Cheap → G2G publishing bot.

Source: raid-cheap.com only (Chinese RSL account marketplace).

Usage:
  python main.py               # continuous loop
  python main.py --once        # single cycle then exit
  python main.py --parse-only  # show accounts without posting (debug)
  python main.py --debug       # verbose logging
"""

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from formatter import format_description, format_title
from g2g_poster import G2GPoster
from parser_raidcheap import RaidCheapScraper, fetch_raidcheap_list
from parser_raidcheap import fetch_raidcheap_account as fetch_detail

PUBLISHED_FILE = Path("published_ids.json")
CONFIG_FILE = Path("config.json")


def _setup_logging(debug: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        handlers=[
            logging.FileHandler("bot.log", encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


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


logger = logging.getLogger(__name__)


async def run_cycle(config: dict, poster: G2GPoster, scraper: RaidCheapScraper) -> None:
    logger.info("=== Scan cycle ===")

    try:
        all_current = await fetch_raidcheap_list(scraper)
    except Exception as exc:
        logger.error(f"Failed to fetch account list: {exc}")
        return

    if not all_current:
        logger.warning("No accounts found on raid-cheap.com")
        return

    all_current.sort(key=lambda x: x[2])  # cheapest first
    logger.info(
        f"Live: {len(all_current)} accounts | "
        f"Cheapest: ${all_current[0][2]:.2f}"
    )

    current_ids = {t[0] for t in all_current}
    current_map = {t[0]: t for t in all_current}

    published = load_published()
    published_ids = set(published.keys())

    new_ids = current_ids - published_ids
    removed_ids = published_ids - current_ids

    logger.info(f"New: {len(new_ids)}  |  Removed: {len(removed_ids)}")

    # ── Publish new accounts (cheapest first) ────────────────────────────────
    new_sorted = [t for t in all_current if t[0] in new_ids]
    for account_id, detail_url, price in new_sorted:
        logger.info(f"Publishing {account_id}  ${price:.2f} …")
        try:
            acc = await fetch_detail(account_id, detail_url, price)
            if acc is None:
                logger.error(f"Could not load data for {account_id} — skip")
                continue

            title = format_title(acc)
            description = format_description(acc)

            lot_id = await poster.create_listing(title, description, price)
            if lot_id:
                published[account_id] = lot_id
                save_published(published)
                logger.info(f"✓ {account_id} → lot {lot_id}  ${price:.2f}")
            else:
                logger.error(f"✗ Failed to create listing for {account_id}")

        except Exception as exc:
            logger.error(f"Error publishing {account_id}: {exc}", exc_info=True)

    # ── Deactivate removed accounts ───────────────────────────────────────────
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
            else:
                logger.error(f"✗ Could not deactivate lot {lot_id}")
        except Exception as exc:
            logger.error(f"Error deactivating {lot_id}: {exc}", exc_info=True)


async def main() -> None:
    ap = argparse.ArgumentParser(description="Raid Cheap → G2G bot")
    ap.add_argument("--once", action="store_true", help="Single cycle then exit")
    ap.add_argument("--parse-only", action="store_true", help="Print accounts, no posting")
    ap.add_argument("--debug", action="store_true", help="Verbose logging")
    args = ap.parse_args()

    _setup_logging(args.debug)
    config = load_config()

    if not PUBLISHED_FILE.exists():
        save_published({})

    async with RaidCheapScraper() as scraper:

        if args.parse_only:
            items = await fetch_raidcheap_list(scraper)
            items.sort(key=lambda x: x[2])
            print(f"\nFound {len(items)} accounts (cheapest first):\n")
            for account_id, detail_url, price in items:
                acc = await fetch_detail(account_id, detail_url, price)
                if not acc:
                    print(f"  {account_id}  ${price:.2f}  ← details unavailable")
                    continue
                print(f"{'─' * 65}")
                print(f"  ID    : {account_id}  |  Price: ${price:.2f}")
                print(f"  Mythic: {', '.join(acc.mythic_champions) or '—'}")
                print(f"  Legs  : {', '.join(acc.legendary_champions[:5]) or '—'}")
                print(f"  TITLE : {format_title(acc)}")
            return

        async with G2GPoster(config) as poster:
            logger.info("Bot ready. Monitoring raid-cheap.com …")

            if args.once:
                await run_cycle(config, poster, scraper)
                return

            while True:
                await run_cycle(config, poster, scraper)
                interval = config.get("check_interval_minutes", 10) * 60
                logger.info(f"Next scan in {interval // 60} min …")
                await asyncio.sleep(interval)


if __name__ == "__main__":
    asyncio.run(main())
