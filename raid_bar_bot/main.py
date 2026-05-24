"""
Raid Bar + Raid Cheap → G2G publishing bot.

Sources:
  • raid.bar/pr      — accounts in English  (httpx scraper)
  • raid-cheap.com   — accounts in Chinese  (Playwright: clicks champions → Search)

Usage:
  python main.py               # continuous loop (both sources)
  python main.py --once        # single cycle then exit
  python main.py --parse-only  # show accounts without posting (debug)
  python main.py --debug       # verbose logging
  python main.py --source bar  # only raid.bar/pr
  python main.py --source cheap # only raid-cheap.com
"""

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from formatter import format_description, format_title
from g2g_poster import G2GPoster
from parser import AccountData, fetch_account_list
from parser import fetch_single_account as fetch_bar_detail
from parser_raidcheap import RaidCheapScraper, fetch_raidcheap_list
from parser_raidcheap import fetch_raidcheap_account as fetch_cheap_detail

PUBLISHED_FILE = Path("published_ids.json")
CONFIG_FILE = Path("config.json")


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _setup_logging(debug: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        handlers=[
            logging.FileHandler("bot.log", encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


# ---------------------------------------------------------------------------
# State
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


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Fetch combined list from all sources (sorted cheapest first)
# ---------------------------------------------------------------------------

async def get_all_current(
    sources: list[str],
    cheap_scraper: RaidCheapScraper | None,
) -> list[tuple[str, str, float, str]]:
    """
    Returns (account_id, detail_url, price_usd, source_tag) for every
    currently-listed account, sorted cheapest first.
    """
    combined: list[tuple[str, str, float, str]] = []

    if "bar" in sources:
        try:
            items = await fetch_account_list()
            for account_id, url, price in items:
                combined.append((account_id, url, price, "bar"))
            logger.info(f"raid.bar/pr: {len(items)} accounts")
        except Exception as exc:
            logger.error(f"raid.bar fetch failed: {exc}")

    if "cheap" in sources and cheap_scraper is not None:
        try:
            items = await fetch_raidcheap_list(cheap_scraper)
            for account_id, url, price in items:
                combined.append((account_id, url, price, "cheap"))
            logger.info(f"raid-cheap.com: {len(items)} accounts")
        except Exception as exc:
            logger.error(f"raid-cheap.com fetch failed: {exc}")

    # Sort cheapest first
    combined.sort(key=lambda x: x[2])
    return combined


async def fetch_full(
    account_id: str, url: str, price: float, source: str
) -> AccountData | None:
    if source == "bar":
        return await fetch_bar_detail(account_id, url, price)
    return await fetch_cheap_detail(account_id, url, price)


# ---------------------------------------------------------------------------
# Main cycle
# ---------------------------------------------------------------------------

async def run_cycle(
    config: dict,
    poster: G2GPoster,
    cheap_scraper: RaidCheapScraper | None,
    sources: list[str],
) -> None:
    logger.info(f"=== Scan cycle | sources: {sources} ===")

    all_current = await get_all_current(sources, cheap_scraper)
    if not all_current:
        logger.warning("No accounts found from any source")
        return

    cheapest = all_current[0]
    logger.info(
        f"Total live: {len(all_current)} | "
        f"Cheapest: ${cheapest[2]:.2f} [{cheapest[3]}] {cheapest[0]}"
    )

    current_ids = {t[0] for t in all_current}
    current_map = {t[0]: t for t in all_current}

    published = load_published()
    published_ids = set(published.keys())

    new_ids = current_ids - published_ids
    removed_ids = published_ids - current_ids

    logger.info(f"New: {len(new_ids)}  |  Removed: {len(removed_ids)}")

    # ── Publish new accounts (cheapest first, already sorted) ────────────────
    new_sorted = [t for t in all_current if t[0] in new_ids]
    for account_id, detail_url, price, source_tag in new_sorted:
        logger.info(f"[{source_tag}] Publishing {account_id}  ${price:.2f} …")
        try:
            acc = await fetch_full(account_id, detail_url, price, source_tag)
            if acc is None:
                logger.error(f"Could not load account data for {account_id} — skip")
                continue

            title = format_title(acc)
            description = format_description(acc)

            lot_id = await poster.create_listing(title, description, price)
            if lot_id:
                published[account_id] = lot_id
                save_published(published)
                logger.info(f"✓ {account_id} → lot {lot_id}  ${price:.2f}")
            else:
                logger.error(f"✗ Listing creation failed for {account_id}")

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


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    ap = argparse.ArgumentParser(description="RSL account → G2G bot")
    ap.add_argument("--once", action="store_true", help="Single cycle then exit")
    ap.add_argument("--parse-only", action="store_true", help="Print accounts, no posting")
    ap.add_argument("--debug", action="store_true", help="Verbose logging")
    ap.add_argument(
        "--source",
        choices=["bar", "cheap", "both"],
        default="both",
        help="Which site to scrape (default: both)",
    )
    args = ap.parse_args()

    _setup_logging(args.debug)
    config = load_config()

    if not PUBLISHED_FILE.exists():
        save_published({})

    sources = ["bar", "cheap"] if args.source == "both" else [args.source]
    use_cheap = "cheap" in sources

    # ── Parse-only mode (no G2G posting) ─────────────────────────────────────
    if args.parse_only:
        async with RaidCheapScraper() as scraper:
            all_items = await get_all_current(sources, scraper if use_cheap else None)
        print(f"\nFound {len(all_items)} accounts (cheapest first):\n")
        for account_id, detail_url, price, source_tag in all_items:
            acc = await fetch_full(account_id, detail_url, price, source_tag)
            tag = f"[{source_tag}]"
            if not acc:
                print(f"  {tag} {account_id}  ${price:.2f}  ← details unavailable")
                continue
            print(f"{'─' * 65}")
            print(f"  {tag}  ID: {account_id}  |  Price: ${price:.2f}")
            print(f"  Mythics : {', '.join(acc.mythic_champions) or '—'}")
            print(f"  Legs    : {', '.join(acc.legendary_champions[:6]) or '—'}")
            print(f"  TITLE   : {format_title(acc)}")
        return

    # ── Normal / loop mode ───────────────────────────────────────────────────
    async with G2GPoster(config) as poster:
        # Open raid-cheap.com browser only when needed
        if use_cheap:
            cheap_scraper_ctx = RaidCheapScraper()
            await cheap_scraper_ctx.__aenter__()
        else:
            cheap_scraper_ctx = None

        try:
            logger.info(f"Bot ready. Sources: {sources}")

            if args.once:
                await run_cycle(config, poster, cheap_scraper_ctx, sources)
                return

            while True:
                await run_cycle(config, poster, cheap_scraper_ctx, sources)
                interval = config.get("check_interval_minutes", 10) * 60
                logger.info(f"Next scan in {interval // 60} min …")
                await asyncio.sleep(interval)

        finally:
            if cheap_scraper_ctx is not None:
                await cheap_scraper_ctx.__aexit__(None, None, None)


if __name__ == "__main__":
    asyncio.run(main())
